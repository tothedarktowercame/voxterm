"""Regression test: a statement under Guide repair is not a stopped campaign.

f216/m03J02 (2026-09-10): the Solver refuted the registered statement, the
frame stopped with :reason :statement-refuted, and the queue handed the
statement to the Guide for one repair (problem_queue_supervisor.clj
statement-repair-handoff). The strip read "f216 STOPPED statement-refuted"
for the whole repair, which says the campaign halted when it had not.

A refutation with no repair in flight must still read STOPPED, so both
directions are checked. Drives the real apm_status() over f216's frozen
ledger and queue state.

Run: python3 test_apm_statement_repair_strip.py
"""
import os, re, shutil, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "apm-statement-repair"
CAMP = "jit-all-open-v3"
root = tempfile.mkdtemp(prefix="s3-repair-")
cdir = os.path.join(root, CAMP)
fdir = os.path.join(cdir, CAMP + "-f216")
os.makedirs(fdir)
# Campaign discovery only; the f193 fixture's copy serves.
shutil.copy2(HERE / "fixtures" / "apm-cascade-strip" / "coordinator.edn", cdir)
shutil.copy2(FIXTURE / "ledger.edn", fdir)
QUEUE = os.path.join(cdir, "queue-state.edn")
real = (FIXTURE / "queue-state.edn").read_text()
NOW = int(re.search(r":dispatch/dispatched-at-ms (\d+)", real).group(1)) / 1000 + 60

fails = []

def run(label, text, want_in, want_not_in=(), want_state=None):
    open(QUEUE, "w").write(text)
    server._APM_PARK_CACHE.clear()
    with patch.object(server, "APM_ROOT", root), \
         patch.object(server.time, "time", return_value=NOW), \
         patch.object(server, "_jvm_health", return_value=None), \
         patch.object(server, "_substrate_permits", return_value=None), \
         patch.object(server, "_apm_running_jobs", return_value=[]), \
         patch.object(server, "urlopen", side_effect=AssertionError("network forbidden")):
        d = server.apm_status()
    st, al = d.get("state"), d.get("alert") or ""
    print("%-22s %-8s | %s" % (label, st, al))
    if want_in not in al:
        fails.append("%s: missing %r in %r" % (label, want_in, al))
    for w in want_not_in:
        if w in al:
            fails.append("%s: still says %r" % (label, w))
    if want_state and st != want_state:
        fails.append("%s: state %r not %r" % (label, st, want_state))

run("f216 repair in flight", real, "Guide repairing m03J02",
    want_not_in=("STOPPED",), want_state="waiting")
run("queue moved on",
    real.replace(":status :voided-slot-awaiting-revision", ":status :advancing"),
    "f216 STOPPED statement-refuted", want_state="stopped")
run("another frame's repair", real.replace(':frame/id "f216"', ':frame/id "f215"'),
    "f216 STOPPED statement-refuted", want_state="stopped")

shutil.rmtree(root, ignore_errors=True)
print()
if fails:
    print("FAIL:"); [print("  -", f) for f in fails]; sys.exit(1)
print("all three directions pass against the real apm_status()")
