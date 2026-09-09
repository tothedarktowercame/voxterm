"""Regression test: every topology campaign is visible, not just the first.

2026-09-09: a second build loop was stood up in the same lab (apm-lean
45f338c3 gave each campaign its own TOPOLOGY_RUNS and TOPOLOGY_LEDGER), and
topology_status() read `runs` and `worklist.edn` by name. The new campaign was
running, dispatching and committing, and the strip showed nothing of it --
which is the condition the first strip was built to end.

Drives the real topology_status() against the real lab rather than a fixture:
the run-directory layout it discovers is the thing under test.

Run: python3 test_topology_tracks.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + ("" if cond else "  <- " + str(detail)))
    if not cond:
        failures.append(name)


# Pinned from the live lab: the second campaign is the (runs2, worklist2.edn)
# pair seeded in apm-lean d0d289a4.
lab = server.TOPO_LAB
second = os.path.join(lab, "worklist2.edn")
if not os.path.isfile(second):
    print("no second campaign at " + second + "; nothing to check")
    sys.exit(0)

found = server._topology_tracks()
ids = [t[0] for t in found]
check("track 1 is the original runs/worklist.edn pair",
      found[0] == ("1", "runs", "worklist.edn"), found[0])
check("track 2 is discovered", "2" in ids, found)
check("a runs<N> without its worklist<N>.edn is not a track",
      all(os.path.isfile(os.path.join(lab, w)) for _, _, w in found), found)

d = server.topology_status()
tracks = d.get("tracks") or []
check("status reports every discovered track", len(tracks) == len(found),
      (len(tracks), len(found)))
check("each track carries its own id",
      sorted(t.get("track") for t in tracks) == sorted(ids),
      [t.get("track") for t in tracks])

named = [t for t in tracks if t.get("ok") and t.get("row")]
check("tracks report distinct rows",
      len({t["row"] for t in named}) == len(named),
      [t.get("row") for t in named])

# The endpoint was flat before there was a second loop; a client that has not
# been updated must still see the first campaign rather than nothing.
check("track 1's fields stay at the top level",
      d.get("track") == "1" and d.get("row") == tracks[0].get("row"),
      (d.get("track"), d.get("row")))

# --- A finished loop is not a silent supervisor ---------------------------
# 2026-09-09: track 1 exited cleanly at 18:59:54 with one :needs-owner row and
# the strip said "supervisor silent 936s" in red for the next quarter of an
# hour. The loop's two clean exits ("PAUSED: no runnable rows", "DONE: no open
# or unreviewed rows") go out through notify() and are never written to
# build-loop.log, which is the only file the exit scan read.
import shutil, tempfile

# Pinned verbatim from runs/supervisor.log and runs/build-loop.log.
FINISHED_SUP = (
    "[2026-09-09T14:39:35Z] supervisor: starting loop (repairs used 0/3)\n"
    "[2026-09-09T18:59:54Z] supervisor: loop exited cleanly (rc=0); "
    "supervisor done\n")
REPAIRED_SUP = (
    "[2026-09-09T13:29:12Z] supervisor: loop exited rc=1 reason='invalid "
    "receipt from invoke-1788960438834-16346-d9147406'\n"
    "[2026-09-09T13:30:20Z] supervisor: healthy after 60s; restarting loop\n"
    "[2026-09-09T13:30:22Z] supervisor: starting loop (repairs used 3/3)\n")
LOOP_LOG = (
    "[2026-09-09T18:59:38Z] review(checkpoint-full-dag-refill-17) done "
    "job=invoke-1788980274475-16845-85960495\n"
    "[2026-09-09T18:59:46Z] applied pass to checkpoint-full-dag-refill-17\n"
    "topology-worklist: 383 items OK; "
    "{:done 376, :needs-owner 1, :superseded 6}\n")
# checked-at is hours stale -- the heartbeat stopped because the loop ended.
HEARTBEAT = ("{:job invoke-1788980274475-16845-85960495 "
             ":checked-at 2026-09-09T18:59:37Z}\n")


def track_from(supervisor_log):
    lab = tempfile.mkdtemp(prefix="topo-fixture-")
    runs = os.path.join(lab, "runs")
    os.makedirs(runs)
    open(os.path.join(lab, "worklist.edn"), "w").write("{:items []}\n")
    open(os.path.join(runs, "supervisor.log"), "w").write(supervisor_log)
    open(os.path.join(runs, "build-loop.log"), "w").write(LOOP_LOG)
    open(os.path.join(runs, "heartbeat.edn"), "w").write(HEARTBEAT)
    saved = server.TOPO_LAB
    server.TOPO_LAB = lab
    try:
        return server._topology_track("1", "runs", "worklist.edn")
    finally:
        server.TOPO_LAB = saved
        shutil.rmtree(lab, ignore_errors=True)


t = track_from(FINISHED_SUP)
check("a clean rc=0 exit reads as finished, not stalled",
      t.get("state") == "finished", t.get("state"))
check("a finished track is not reported as a silent supervisor",
      "silent" not in (t.get("alert") or ""), t.get("alert"))
check("the alert names the decision the track is waiting for",
      "owner decision" in (t.get("alert") or ""), t.get("alert"))
check("the stale heartbeat is still reported as a number",
      isinstance(t.get("heartbeat_s"), int), t.get("heartbeat_s"))

t = track_from(REPAIRED_SUP)
check("a loop restarted after a repairable failure is not called finished",
      t.get("state") not in ("finished", "stopped"), t.get("state"))

print("\nRESULT: " + ("OK" if not failures else "FAILURES: " + str(failures)))
sys.exit(1 if failures else 0)
