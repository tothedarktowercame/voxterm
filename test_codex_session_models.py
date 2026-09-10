import json, os, shutil, tempfile
import server

root = tempfile.mkdtemp(prefix="voxterm-codex-model-")
saved = server.CODEX_SESSIONS_ROOT
server.CODEX_SESSIONS_ROOT = root
server._CODEX_SESSION_MODEL_CACHE.clear()
failures = []

def check(name, condition):
    print(("PASS " if condition else "FAIL ") + name)
    if not condition: failures.append(name)

def log(sid, records, trailing=b""):
    d = os.path.join(root, "2026", "09", "10"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rollout-x-%s.jsonl" % sid)
    with open(p, "wb") as h:
        for record in records: h.write(json.dumps(record).encode() + b"\n")
        h.write(trailing)
    return p

def turn(model): return {"type":"turn_context", "payload":{"model":model}}

try:
    a = log("seat-a", [turn("gpt-6-astra")])
    log("seat-b", [turn("gpt-5.6-sol")])
    check("two seats keep different models", server.codex_session_model("seat-a") == "gpt-6-astra" and server.codex_session_model("seat-b") == "gpt-5.6-sol")
    with open(a, "ab") as h: h.write(json.dumps(turn("gpt-6-astra-plus")).encode() + b"\n")
    check("model change in one session is observed", server.codex_session_model("seat-a") == "gpt-6-astra-plus")
    check("missing session is unknown", server.codex_session_model("missing") is None)
    log("partial", [turn("gpt-6-astra")], b'{"type":"turn_context","payload":')
    check("partial trailing JSON keeps last complete turn", server.codex_session_model("partial") == "gpt-6-astra")
finally:
    server.CODEX_SESSIONS_ROOT = saved
    server._CODEX_SESSION_MODEL_CACHE.clear()
    shutil.rmtree(root)

raise SystemExit(1 if failures else 0)
