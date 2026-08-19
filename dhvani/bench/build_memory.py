"""Measure the build's memory profile — parent and workers, sampled.

The full-subset build was OOM-killed twice (change log, 15 Aug) and both times
the post-mortem was a kernel message rather than a measurement. Worker count and
row count are the two dials and neither can be set from the 300-row build's
numbers, because that run's memory profile is not a scaled-down version of the
full one: the parent grows with rows, the workers do not.

    python -m dhvani.bench.build_memory --rows 300 --workers 8

Runs the real build as a subprocess and samples `VmRSS` for the parent and every
descendant, plus `MemAvailable`, until it exits. Reports the peaks, which are
what the dial has to be set against — the constraint is

    parent_peak + workers * worker_peak < MemTotal   (this box has no swap)

Sampling is `/proc` rather than psutil: one dependency fewer on the deploy box,
and VmRSS is the number the OOM killer's `anon-rss` line reports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROC = Path("/proc")


def _rss_kb(pid: int) -> int:
    """Resident set of one process, or 0 if it exited mid-sample."""
    try:
        for line in (PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return 0


def _mem_available_kb() -> int:
    for line in (PROC / "meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return 0


def _descendants(root: int) -> list[int]:
    """Every live descendant of `root`, via each process's `children` file.

    Reading `children` per process beats scanning all of /proc for ppid: the
    build has ~8 workers and the box has hundreds of unrelated processes.
    """
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        try:
            kids = (PROC / str(pid) / "task" / str(pid) / "children").read_text().split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        for k in kids:
            out.append(int(k))
            stack.append(int(k))
    return out


def sample(proc: subprocess.Popen, interval: float = 0.5) -> dict:
    """Poll until `proc` exits. Returns peaks and a downsampled timeline."""
    peak_parent = 0
    peak_worker = 0            # largest single worker seen
    peak_workers_sum = 0       # largest combined worker footprint seen
    peak_total = 0
    max_workers_live = 0
    min_avail = _mem_available_kb()
    timeline = []
    t0 = time.perf_counter()

    while proc.poll() is None:
        parent = _rss_kb(proc.pid)
        kids = _descendants(proc.pid)
        rss = [_rss_kb(p) for p in kids]
        rss = [r for r in rss if r]
        total = parent + sum(rss)
        avail = _mem_available_kb()

        peak_parent = max(peak_parent, parent)
        peak_worker = max(peak_worker, max(rss, default=0))
        peak_workers_sum = max(peak_workers_sum, sum(rss))
        peak_total = max(peak_total, total)
        max_workers_live = max(max_workers_live, len(rss))
        min_avail = min(min_avail, avail)
        timeline.append({
            "t": round(time.perf_counter() - t0, 1),
            "parent_gb": round(parent / 1048576, 2),
            "workers": len(rss),
            "workers_gb": round(sum(rss) / 1048576, 2),
            "avail_gb": round(avail / 1048576, 2),
        })
        time.sleep(interval)

    gb = lambda kb: round(kb / 1048576, 2)  # noqa: E731
    return {
        "seconds": round(time.perf_counter() - t0, 1),
        "returncode": proc.returncode,
        "peak_parent_gb": gb(peak_parent),
        "peak_worker_gb": gb(peak_worker),
        "peak_workers_sum_gb": gb(peak_workers_sum),
        "peak_total_gb": gb(peak_total),
        "min_mem_available_gb": gb(min_avail),
        "max_workers_live": max_workers_live,
        # One sample a second is plenty for a run measured in minutes, and keeps
        # the evidence file readable.
        "timeline": timeline[::max(1, int(1.0 / interval))],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=300)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--langs", nargs="*", default=[],
                    help="default is whatever the build defaults to — every "
                         "corpus. A profiler that quietly measures one corpus "
                         "misses the accumulation across them, which is the "
                         "part of the parent's footprint that grows.")
    ap.add_argument("--build-out", default="",
                    help="index dir for the probe build; default is a temp dir "
                         "so a profiling run never overwrites index/")
    ap.add_argument("--out", default="", help="evidence JSON path")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--build-args", nargs="*", default=[],
                    help="passed through to build_index verbatim, e.g. "
                         "--build-args --no-cpu-mem-arena")
    args = ap.parse_args()

    build_out = Path(args.build_out) if args.build_out else Path(
        f"/tmp/dhvani-membuild-{args.rows}r-{args.workers}w")
    build_out.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "dhvani.build.build_index",
           "--rows", str(args.rows), "--workers", str(args.workers),
           "--threads", str(args.threads), "--out", str(build_out)]
    if args.langs:
        cmd += ["--langs", *args.langs]
    cmd += args.build_args
    print(" ".join(cmd), flush=True)

    total_kb = int(next(l for l in (PROC / "meminfo").read_text().splitlines()
                        if l.startswith("MemTotal:")).split()[1])
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Kept, not discarded: a profiling run that dies tells you the peak but not
    # the reason, and "the build ran blind" is already in the change log once.
    log_path = build_out / "build.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        result = sample(proc, args.interval)
    tail = log_path.read_text().splitlines()[-15:]
    if result["returncode"] != 0:
        result["failure_tail"] = tail
        print("\n".join(tail), flush=True)

    manifest = build_out / "manifest.json"
    chunks = None
    if manifest.exists():
        chunks = json.loads(manifest.read_text())["totals"]["chunks"]

    report = {
        "run_utc": t0,
        "config": {"rows": args.rows, "workers": args.workers,
                   "threads": args.threads, "langs": args.langs,
                   "mem_total_gb": round(total_kb / 1048576, 2),
                   "swap": False},
        "chunks": chunks,
        **result,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "timeline"}, indent=2))

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"wrote {args.out}", flush=True)
    return 0 if result["returncode"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
