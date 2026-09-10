"""Regression test: a watchdog that halted the coordinator is not "silent".

2026-09-10 17:33:53Z: the semantic-progress watchdog stopped the APM
coordinator because a tick intent passed its deadline, and that tick had
failed :problem-queue-state-plan-mismatch. A halted watchdog stops observing,
so its observed-at aged like a dead one's and the strip read "watchdog silent
58m -- loop supervisor gone" over a record that named both causes.

A watchdog that is still :watching but has stopped updating is the genuine
dead-supervisor case and must keep that alarm, so both directions are
checked. Drives the real apm_status() over the halted record's frozen text.

Run: python3 test_apm_watchdog_halt_strip.py
"""
import os, re, shutil, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "apm-watchdog-halt"
CASCADE = HERE / "fixtures" / "apm-cascade-strip"
CAMP = "jit-all-open-v3"
root = tempfile.mkdtemp(prefix="s3-halt-")
cdir = os.path.join(root, CAMP)
fdir = os.path.join(cdir, CAMP + "-f193")
os.makedirs(fdir)
# Campaign discovery and an in-progress frame (last event :frame/advanced);
# neither is what is under test, so the f193 fixture's copies serve.
shutil.copy2(CASCADE / "coordinator.edn", cdir)
shutil.copy2(CASCADE / "ledger.edn", fdir)
WD = os.path.join(cdir, "coordinator.edn.watchdog.edn")
real = (FIXTURE / "coordinator.edn.watchdog.edn").read_text()
# The live strip read "58m" at 18:32; reproduce that distance.
NOW = int(re.search(r":watchdog/observed-at-ms (\d+)", real).group(1)) / 1000 + 3501

fails = []

def run(label, text, want_in, want_not_in=(), want_state=None):
    open(WD, "w").write(text)
    with patch.object(server, "APM_ROOT", root), \
         patch.object(server.time, "time", return_value=NOW), \
         patch.object(server, "_jvm_health", return_value=None), \
         patch.object(server, "_substrate_permits", return_value=None), \
         patch.object(server, "_apm_running_jobs", return_value=[]), \
         patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")):
        d = server.apm_status()
    st, al = d.get("state"), d.get("alert") or ""
    print("%-18s %-8s | %s" % (label, st, al))
    if want_in not in al:
        fails.append("%s: missing %r in %r" % (label, want_in, al))
    for w in want_not_in:
        if w in al:
            fails.append("%s: still says %r" % (label, w))
    if want_state and st != want_state:
        fails.append("%s: state %r not %r" % (label, st, want_state))

run("17:33 halt", real,
    "coordinator stopped by watchdog 17:33Z: external-job-deadline-exceeded"
    " (last tick: problem-queue-state-plan-mismatch)",
    want_not_in=("supervisor gone",), want_state="stopped")
run("stale, not halted",
    real.replace(":watchdog/status :halted", ":watchdog/status :watching"),
    "loop supervisor gone", want_state="stalled")

shutil.rmtree(root, ignore_errors=True)
print()
if fails:
    print("FAIL:"); [print("  -", f) for f in fails]; sys.exit(1)
print("both directions pass against the real apm_status()")
