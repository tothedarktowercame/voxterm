"""Regression tests for Emacs-to-browser microphone toggle delivery."""
import http.client
import os
import sys
import threading

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


# Exact bad case: Emacs sends `{}` and reuses its HTTP connection.  Before the
# handler consumed that body, the second request began with the unread bytes
# and BaseHTTPRequestHandler parsed "{}POST" as its method (501).
server._mic_toggles = 0
server._mic_owner = None
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
try:
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
    headers = {"Content-Type": "application/json"}
    for expected_depth in (1, 2):
        connection.request("POST", "/mic-toggle", body="{}", headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200, (response.status, body)
        assert '"depth": %d' % expected_depth in body, body
    connection.close()
    assert server.take_mic_toggle("phone", False) == (True, 1)
    assert server.take_mic_toggle("phone", False) == (True, 0)
finally:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)
