# Hugging Face Space, Docker SDK. ADR-034 — the deploy target, replacing the
# Lightsail VM ADR-010 chose, because that needed a credit card and this does
# not, and because ADR-033 got the footprint under a free tier's ceiling.
FROM python:3.11-slim

# libgomp1: OpenMP runtime, required by both faiss-cpu and onnxruntime. Without
# it the image builds and then fails at import, which is a slow way to find out.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned exactly as the dev box runs them, so the numbers in docs/results/ and
# the numbers this serves come from the same versions (requirements.txt).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Spaces run the container as uid 1000 and nothing outside $HOME is writable.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/user/.cache/huggingface \
    DHVANI_INDEX_DIR=/home/user/index \
    DHVANI_ONNX_THREADS=2

WORKDIR /home/user/app
COPY --chown=user dhvani ./dhvani
COPY --chown=user web ./web
COPY --chown=user space_boot.py ./space_boot.py

# 7860 is the port Spaces expects unless `app_port` says otherwise (README.md).
EXPOSE 7860
CMD ["python", "space_boot.py"]
