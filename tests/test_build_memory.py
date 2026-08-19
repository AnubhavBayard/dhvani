"""The sampler is only useful if it sees the workers, not just the parent.

The OOM post-mortems turned on per-worker footprint, so a sampler that silently
missed descendants would report a comfortable-looking peak and set the dial
wrong. Both processes here allocate a known amount and hold it.
"""

from __future__ import annotations

import subprocess
import sys

from dhvani.bench.build_memory import _descendants, _rss_kb, sample

# Parent allocates 200 MB, spawns a child that allocates 200 MB, both hold ~3 s.
# bytearray, not a list: one contiguous allocation with no per-object overhead,
# so the number asserted against is the number requested.
CHILD = "import time; b=bytearray(200*1024*1024); b[::4096]=b'x'*len(b[::4096]); time.sleep(3)"
PARENT = (
    "import subprocess,sys,time;"
    f"c=subprocess.Popen([sys.executable,'-c',{CHILD!r}]);"
    "b=bytearray(200*1024*1024); b[::4096]=b'x'*len(b[::4096]);"
    "time.sleep(3); c.wait()"
)


def test_sampler_sees_parent_and_descendant():
    proc = subprocess.Popen([sys.executable, "-c", PARENT])
    r = sample(proc, interval=0.2)

    assert r["returncode"] == 0
    assert r["max_workers_live"] >= 1, "descendant never observed"
    # 200 MB is 0.19 GB; allow the interpreter's own footprint but require that
    # the bulk of the allocation was actually seen.
    assert r["peak_parent_gb"] >= 0.15, r
    assert r["peak_worker_gb"] >= 0.15, r
    assert r["peak_total_gb"] >= r["peak_parent_gb"] + 0.15, r
    assert r["min_mem_available_gb"] > 0


def test_rss_of_dead_pid_is_zero_not_an_exception():
    # Workers exit while the pool is shutting down; a sample landing there must
    # not take the profiler down with it.
    assert _rss_kb(2**22) == 0
    assert _descendants(2**22) == []
