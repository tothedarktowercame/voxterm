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

print("\nRESULT: " + ("OK" if not failures else "FAILURES: " + str(failures)))
sys.exit(1 if failures else 0)
