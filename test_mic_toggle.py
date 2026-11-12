"""Regression test for Emacs-to-browser microphone toggle ownership."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

server._mic_toggles = 0
server._mic_owner = None
assert server.take_mic_toggle("phone", False) == (False, 0)
assert server.queue_mic_toggle() == 1
assert server.take_mic_toggle("stale", True) == (False, 1)
assert server.take_mic_toggle("phone", True) == (True, 0)
server.queue_mic_toggle()
server.queue_mic_toggle()
assert server.take_mic_toggle("phone", True) == (True, 1)
assert server.take_mic_toggle("phone", True) == (True, 0)
print("ok")
