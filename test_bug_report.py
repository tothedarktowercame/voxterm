"""Small contract tests for the Voxterm -> Emacs bug-report handoff."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


assert server.capture_bug_report("   ") == {"ok": False, "detail": "empty bug report"}

with mock.patch("server.subprocess.run") as run:
    run.return_value = mock.Mock(returncode=0, stdout="(:accepted t :process 42)\n", stderr="")
    result = server.capture_bug_report('button said "surprise"')
    assert result["ok"] is True, result
    command = run.call_args.args[0]
    assert command[:4] == ["emacsclient", "-s", server.EMACS_SOCKET, "-e"]
    assert "report-futon-bug-async" in command[4]
    assert r'button said \"surprise\"' in command[4]

print("ok")
