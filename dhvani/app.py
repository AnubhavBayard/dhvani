"""FastAPI app — POST /stt, POST /ask (SSE), static UI, health.

`WS /ws/stt` — streaming audio with partial transcripts feeding speculative
retrieval — is deferred (ADR-029). `POST /stt` takes the whole recording once
the user stops talking. That costs the overlap between talking and retrieving,
which is boundary C, reported and not targeted; it buys voice working today.

The index loads once in the lifespan handler and is warmed with a throwaway
query before the app reports ready, so no cold start lands in a measured
percentile and the first real user does not pay 5 s (README, benchmark hygiene).

    uvicorn dhvani.app:app                    # index/full, 2 threads
    DHVANI_INDEX_DIR=index uvicorn dhvani.app:app
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dhvani.harness.contracts import PipelineTrace
from dhvani.pipeline import BOUNDARY_A_COVERS, NOT_YET_IN_BOUNDARY_A, Dhvani
from dhvani.stt.base import MAX_AUDIO_BYTES, STT, STTConfig, STTUnavailable

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_QUERY_CHARS = 500        # The length bound belongs at the trust boundary
                             # whatever L1 does with the text afterwards
                             # (guardrails/checks.py owns everything else)

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    index_dir = os.environ.get("DHVANI_INDEX_DIR", "index/full")
    threads = int(os.environ.get("DHVANI_ONNX_THREADS", "2"))
    if os.environ.get("DHVANI_SKIP_INDEX") != "1":
        d = Dhvani.load(index_dir, threads=threads)
        d.warm()
        state["dhvani"] = d
    state["index_dir"] = index_dir
    state["stt"] = STT(STTConfig(
        provider=os.environ.get("DHVANI_STT_PROVIDER", "sarvam")))
    yield
    state.clear()


app = FastAPI(title="dhvani", lifespan=lifespan)


class AskRequest(BaseModel):
    q: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/health")
def health() -> dict:
    d = state.get("dhvani")
    stt = state.get("stt")
    from dhvani.stt.base import available
    return {"ok": d is not None, "index": state.get("index_dir"),
            "chunks": len(d.store) if d else 0,
            "stt": {p.name: available(p) for p in stt.providers.values()}
                   if stt else {},
            "boundary_a_covers": BOUNDARY_A_COVERS,
            "not_yet_in_boundary_a": NOT_YET_IN_BOUNDARY_A}


@app.post("/stt")
async def transcribe(file: UploadFile = File(...)) -> dict:
    """Audio in, transcript out. Deliberately separate from `/ask`: the user
    sees what was heard and can correct it before it is answered, and boundary
    A's clock starts at the final transcript, not at the microphone."""
    stt = state.get("stt")
    if stt is None:
        raise HTTPException(503, "stt not configured")
    audio = await file.read(MAX_AUDIO_BYTES + 1)
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, f"audio over {MAX_AUDIO_BYTES} bytes")
    trace = PipelineTrace()
    try:
        # Blocking HTTP to the provider; off the event loop for the same reason
        # /ask is.
        tr = await asyncio.to_thread(stt.transcribe, audio,
                                     file.filename or "audio.webm", trace)
    except STTUnavailable as exc:
        # 200, not an error status: "I could not hear that" is a product state
        # the UI renders, and the text box stays available either way.
        return {"ok": False, "reason": str(exc),
                "stages": [s.model_dump() for s in trace.stages]}
    return {"ok": True, **tr.model_dump(),
            "stages": [s.model_dump() for s in trace.stages]}


@app.post("/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    d = state.get("dhvani")
    if d is None:
        raise HTTPException(503, "index not loaded")

    async def body():
        # The pipeline is synchronous and CPU-bound (ONNX, FAISS, BM25). Run it
        # on a worker thread so one query cannot stall the event loop for every
        # other connection — the queue is what turns a 13 ms P50 into a 13 ms
        # P50 under concurrency.
        it = d.answer(req.q)
        while True:
            event = await asyncio.to_thread(next, it, None)
            if event is None:
                return
            yield sse(event)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/")
def root() -> FileResponse:
    return FileResponse(WEB / "index.html")


if WEB.is_dir():
    app.mount("/static", StaticFiles(directory=WEB), name="static")
