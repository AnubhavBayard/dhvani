"""Every entry point the README tells a reader to run must render `--help`.

Found by the fresh-clone verification (21 Aug): `dhvani.build.build_index --help`
crashed with `ValueError: unsupported format character 't'` because an
`add_argument` help string said `2.5% throughput` and argparse runs help strings
through `%`-interpolation. Nothing else caught it — the flag itself worked, and
the build was always run with explicit arguments. `--help` is the first thing
someone types against an unfamiliar CLI, so it is the last place a crash should
live.
"""
import subprocess
import sys

import pytest

# The modules README.md documents as runnable, plus the build entry point.
ENTRY_POINTS = [
    "dhvani.build.build_index",
    "dhvani.build.probe_dataset",
    "dhvani.bench.benchmark",
    "dhvani.bench.adversarial",
]


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_help_renders(module: str) -> None:
    r = subprocess.run([sys.executable, "-m", module, "--help"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{module} --help exited {r.returncode}:\n{r.stderr}"
    assert "usage:" in r.stdout
