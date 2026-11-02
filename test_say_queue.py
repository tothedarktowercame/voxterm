"""Regression test: reading the terminal must not lose an agent's spoken reply.

Bad case (2026-11-01): Joe dictated, switched away, no page polled for 323 s,
and the Gist expired under the 90 s TTL. Run: python3 test_say_queue.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


def reset(*items):
    server._say_queue[:] = [{"text": t, "ts": ts} for t, ts in items]
    server._say_owner = None
    server._say_last_poll = None


reset()
assert server.take_say("a", False, 1000)[0] is None
# Queued at 1001, page away until 1324: still spoken on return.
server._say_queue.append({"text": "gist", "ts": 1001})
item, _ = server.take_say("a", False, 1324)
assert item and item["text"] == "gist", item

# Polled throughout, nobody took it for longer than the TTL: dropped as before.
reset(("old", 1000))
t = 1000
while t < 1100:
    server._say_owner = "other"
    server.take_say("stale", True, t)  # refused, and does not count as listening
    t += 1
server._say_last_poll = 1099
assert server.take_say("a", False, 1100)[0] is None

# The page last visible keeps draining while hidden; a stale tab does not.
reset(("x", 1000), ("y", 1000))
server.take_say("a", False, 1000)  # takes x
assert server.take_say("stale", True, 1001)[0] is None
assert server.take_say("a", True, 1002)[0]["text"] == "y"
print("ok")
