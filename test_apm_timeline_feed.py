"""Regression test: an unreadable Agency job feed must not take down the strip,
and must not be reported as the loop working in-process.

2026-09-09: Joe saw the APM strip replaced by "'NoneType' object is not
iterable" during the topology loop's repair burst. _apm_running_jobs returns
None on any Agency failure -- a 2.5s timeout is enough -- and
_apm_frame_timeline iterated it, so the TypeError propagated out of
apm_status() and the handler rendered the whole strip as that error. The
function's own comment calls the feed "decoration only, never the verdict";
it had become the verdict.

Guarding the loop alone would trade the crash for a quieter wrong answer: both
render branches in index.html fall back to "in-process", which asserts the loop
is doing work that an unread feed does not establish. So the unreadable case
gets its own actor value.

Runs against a real frame directory rather than a fixture: the ledger shape
this parses is the thing under test.

Run: python3 test_apm_timeline_feed.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

CDIR = "/home/joe/code/futon3c/data/apm-campaigns/jit-all-open-v3"
CAMP = "jit-all-open-v3"

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  <- " + str(detail)))
    if not cond:
        failures.append(name)


def a_live_frame():
    """The newest frame with a ledger -- the one the strip actually reads."""
    frames = server._apm_frame_dirs(CDIR, CAMP)
    for num, fdir in reversed(frames):
        if os.path.exists(os.path.join(fdir, "ledger.edn")):
            return num, fdir
    raise SystemExit("no frame with a ledger under " + CDIR)


num, fdir = a_live_frame()
frame = "f%d" % num
print("frame under test: " + frame + " (" + fdir + ")")

original = server._apm_running_jobs
try:
    # 1. Feed unreadable -- what _apm_running_jobs returns on any exception.
    server._apm_running_jobs = lambda: None
    try:
        timeline = server._apm_frame_timeline(fdir, CAMP, frame)
        raised = None
    except Exception as exc:                       # noqa: BLE001 - it IS the bug
        timeline, raised = None, exc
    check("unreadable feed does not raise", raised is None, raised)
    cur = [p for p in (timeline or []) if p.get("current")]
    check("unreadable feed still yields the current phase", bool(cur), timeline)
    if cur:
        check("unreadable feed is not reported as in-process",
              cur[0].get("actor") == "unknown", cur[0].get("actor"))
        check("unreadable feed names no agent", cur[0].get("agent") is None,
              cur[0].get("agent"))

    # 2. Empty feed provides no evidence of internal execution.
    server._apm_running_jobs = lambda: []
    timeline = server._apm_frame_timeline(fdir, CAMP, frame)
    cur = [p for p in (timeline or []) if p.get("current")]
    check("empty feed alone does not claim execution",
          bool(cur) and cur[0].get("actor") == "unknown",
          cur[0].get("actor") if cur else timeline)

    # 3. Feed readable with this frame's own turn -- unchanged behaviour.
    server._apm_running_jobs = lambda: [{"agent": frame + "-solver", "for_s": 42}]
    timeline = server._apm_frame_timeline(fdir, CAMP, frame)
    cur = [p for p in (timeline or []) if p.get("current")]
    check("a live turn is still attributed to its agent",
          bool(cur) and cur[0].get("actor") == "agent"
          and cur[0].get("agent", {}).get("agent") == frame + "-solver",
          cur[0].get("agent") if cur else timeline)
finally:
    server._apm_running_jobs = original

print("\nRESULT: " + ("OK" if not failures else "FAILURES: " + str(failures)))
sys.exit(1 if failures else 0)
