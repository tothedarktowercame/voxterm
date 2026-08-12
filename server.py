#!/usr/bin/env python3
"""voxterm - push-to-talk voice terminal.

Browser captures mic -> 16 kHz mono WAV -> POST here -> whisper.cpp -> text back.

Stdlib only, no venv. Binds to loopback by default: reach it from the phone with
an ssh tunnel (see README), which also satisfies the browser's secure-context
requirement for getUserMedia without any TLS setup.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
WHISPER = os.path.expanduser("~/tools/whisper/whisper-cli")
MODELS = {
    "small.en": os.path.expanduser("~/models/whisper/ggml-small.en.bin"),
    "large-v3-turbo": os.path.expanduser("~/models/whisper/ggml-large-v3-turbo.bin"),
}

# llama-glm already runs with -t 30 on a 32-thread box, so stay modest by default.
THREADS = int(os.environ.get("VOXTERM_THREADS", "12"))
HOST = os.environ.get("VOXTERM_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOXTERM_PORT", "8081"))
MAX_BYTES = 25 * 1024 * 1024

# Decoder bias. A bare word list does NOT work — "Claude" listed alongside
# "Clojure" makes it worse (measured: "Tell Claude" -> "Tell Clojure"). Showing
# each word in the syntactic position it actually occurs is what fixes it.
DEFAULT_PROMPT = ("Ask Claude. Tell Claude. Claude Code. Claude writes Clojure, "
                  "elisp, Emacs, nREPL, futon3c, voxterm, tmux.")
PROMPT = os.environ.get("VOXTERM_PROMPT", DEFAULT_PROMPT)

# Backstop for residue the prompt doesn't catch. Keep this list *small* and only
# unambiguous: "call" and "Clojure" are also common mishearings of "Claude" but
# are real words here, so they must not be substituted.
FIXUPS = [
    (re.compile(r"\bquad\b", re.I), "Claude"),
    (re.compile(r"\bfuton\s*3\s*c\b", re.I), "futon3c"),
    (re.compile(r"\bem\s*axe?\b", re.I), "Emacs"),
]


def apply_fixups(text):
    for pattern, replacement in FIXUPS:
        text = pattern.sub(replacement, text)
    return text

# Where dispatched ("rocket") text goes: emacs | tmux | none
SINK = os.environ.get("VOXTERM_SINK", "emacs")
EMACS_SOCKET = os.environ.get("VOXTERM_EMACS_SOCKET", "server")
TMUX_TARGET = os.environ.get("VOXTERM_TMUX_TARGET", "main:1")
TMUX_ENTER = os.environ.get("VOXTERM_TMUX_ENTER", "0") == "1"
# Press RET after inserting, so dictation actually submits (e.g. claude-repl).
EMACS_SUBMIT = os.environ.get("VOXTERM_EMACS_SUBMIT", "1") == "1"
ELISP = os.path.join(HERE, "voxterm.el")

# Turn-start acknowledgement: speak the transcript back so a mishearing is
# caught before the agent acts on it.
TTS_DIR = os.path.expanduser("~/code/tts")
PIPER = os.path.join(TTS_DIR, "bin", "piper")
VOICE = os.path.join(TTS_DIR, "voices", "en_GB-semaine-medium.onnx")
MAX_SPEAK_CHARS = 600

# Queue of agent text waiting to be spoken. Emacs enqueues (POST /say); the page
# drains it (GET /say/next) because the box has no speaker — the phone does.
_say_lock = threading.Lock()
_say_queue = []
MAX_SAY_QUEUE = 8

# Paragraphs that open with one of these are skipped: unspeakable, and the
# buffer already shows them.
_UNSPEAKABLE = re.compile(r"^\s*(```|~~~|\||#{1,6}\s|>\s)")


def sanitize_for_speech(text):
    """Markdown-ish prose -> something piper can read. '' means don't speak."""
    if not text or _UNSPEAKABLE.match(text):
        return ""
    s = text
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)      # [label](url) -> label
    s = re.sub(r"https?://\S+", " ", s)                  # bare URLs
    s = re.sub(r"`+([^`]*)`+", r"\1", s)                 # inline code fences
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)             # **bold**
    s = re.sub(r"(?<![\w.])/[\w./-]*/([\w.-]+)", r"\1", s)  # /a/b/c.py -> c.py
    s = re.sub(r"^\s*[-*+]\s+", "", s)                   # leading bullet
    s = re.sub(r"^\s*\d+\.\s+", "", s)                   # leading "1. "
    s = s.replace("*", " ").replace("`", " ")            # stray markup
    s = re.sub(r"\s+", " ", s).strip()
    # Nothing but punctuation/markup left is not worth speaking.
    return s if re.search(r"[A-Za-z]{2}", s) else ""

# whisper is CPU-bound; running two at once just thrashes.
_gpu_lock = threading.Lock()


def transcribe(wav_bytes, model_key="small.en", audio_ctx=0, greedy=True):
    model = MODELS.get(model_key, MODELS["small.en"])
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxterm-")
    os.write(fd, wav_bytes)
    os.close(fd)
    try:
        with wave.open(path) as w:
            duration = w.getnframes() / float(w.getframerate())

        cmd = [WHISPER, "-m", model, "-f", path, "-t", str(THREADS),
               "-nt", "-np", "-l", "en", "-sns"]
        if PROMPT:
            cmd += ["--prompt", PROMPT]
        if audio_ctx:
            cmd += ["-ac", str(audio_ctx)]
        if greedy:
            cmd += ["-bs", "1", "-bo", "1"]

        queued = time.monotonic()
        with _gpu_lock:
            started = time.monotonic()
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            finished = time.monotonic()

        text = " ".join(ln.strip() for ln in proc.stdout.splitlines() if ln.strip())
        text = apply_fixups(text)
        return {
            "text": text,
            "model": model_key,
            "audio_ctx": audio_ctx,
            "greedy": greedy,
            "threads": THREADS,
            "audio_sec": round(duration, 2),
            "wait_ms": int((started - queued) * 1000),
            "infer_ms": int((finished - started) * 1000),
            "rtf": round((finished - started) / duration, 2) if duration else None,
            "stderr": proc.stderr[-400:] if proc.returncode != 0 else "",
            "ok": proc.returncode == 0,
        }
    finally:
        os.unlink(path)


def elisp_string(s):
    """Quote TEXT as an elisp string literal."""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    return '"%s"' % out


def route(text, sink=None, submit=None):
    """Send dispatched text to its destination. Returns a small status dict."""
    sink = sink or SINK
    if submit is None:
        submit = EMACS_SUBMIT
    if not text.strip():
        return {"ok": False, "sink": sink, "detail": "empty text"}

    if sink == "none":
        return {"ok": True, "sink": "none", "detail": "not routed"}

    if sink == "emacs":
        # Load voxterm.el on demand so there is nothing to add to init.el.
        expr = ('(progn (unless (fboundp (quote voxterm-insert)) (load %s t t))'
                ' (voxterm-insert %s %s))'
                % (elisp_string(ELISP), elisp_string(text), "t" if submit else "nil"))
        cmd = ["emacsclient", "-s", EMACS_SOCKET, "-e", expr]
    elif sink == "tmux":
        cmd = ["tmux", "send-keys", "-t", TMUX_TARGET, "-l", "--", text]
    else:
        return {"ok": False, "sink": sink, "detail": "unknown sink"}

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {"ok": False, "sink": sink, "detail": repr(e)}

    if sink == "tmux" and p.returncode == 0 and TMUX_ENTER:
        subprocess.run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"],
                       capture_output=True, timeout=15)

    detail = (p.stdout or p.stderr or "").strip().strip('"')
    return {"ok": p.returncode == 0, "sink": sink, "detail": detail[:300]}


def synthesize(text):
    """Render TEXT to a 22 kHz WAV with piper. Returns the bytes."""
    text = text.strip()[:MAX_SPEAK_CHARS]
    if not text:
        raise ValueError("empty text")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxterm-tts-")
    os.close(fd)
    try:
        # cwd matters: piper resolves its bundled espeak-ng data relative to
        # the install tree, not to the model path.
        proc = subprocess.run(
            [PIPER, "-m", VOICE, "-c", VOICE + ".json", "-f", path],
            input=text, capture_output=True, text=True, timeout=60, cwd=TTS_DIR)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "piper failed")[-300:])
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, str(e), "text/plain")
        elif path == "/say/next":
            with _say_lock:
                item = _say_queue.pop(0) if _say_queue else None
                depth = len(_say_queue)
            self._send(200, json.dumps({"text": item, "depth": depth}),
                       "application/json")
        elif path == "/health":
            ok = os.path.exists(WHISPER)
            models = {k: os.path.exists(v) for k, v in MODELS.items()}
            self._send(200, json.dumps({"whisper": ok, "models": models,
                                        "threads": THREADS}), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/transcribe", "/route", "/speak", "/say"):
            self._send(404, "not found", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(413, json.dumps({"error": "bad length"}), "application/json")
            return

        body = self.rfile.read(length)
        q = parse_qs(parsed.query)

        if parsed.path == "/say":
            try:
                payload = json.loads(body.decode("utf-8"))
                spoken = sanitize_for_speech((payload.get("text") or "").strip())
                if not spoken:
                    self._send(200, json.dumps({"ok": True, "queued": False,
                                                "reason": "unspeakable"}),
                               "application/json")
                    return
                with _say_lock:
                    # Stale speech is worse than dropped speech — keep it shallow.
                    if len(_say_queue) >= MAX_SAY_QUEUE:
                        _say_queue.pop(0)
                    _say_queue.append(spoken)
                    depth = len(_say_queue)
                self._send(200, json.dumps({"ok": True, "queued": True,
                                            "chars": len(spoken), "depth": depth}),
                           "application/json")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "detail": repr(e)}),
                           "application/json")
            return

        if parsed.path == "/speak":
            try:
                payload = json.loads(body.decode("utf-8"))
                started = time.monotonic()
                wav = synthesize(payload.get("text", ""))
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.send_header("X-Synth-Ms", str(int((time.monotonic() - started) * 1000)))
                self.end_headers()
                self.wfile.write(wav)
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "detail": repr(e)}),
                           "application/json")
            return

        if parsed.path == "/route":
            try:
                payload = json.loads(body.decode("utf-8"))
                result = route(payload.get("text", ""), payload.get("sink"),
                               payload.get("submit"))
                self._send(200, json.dumps(result), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "detail": repr(e)}),
                           "application/json")
            return

        model = (q.get("model") or ["small.en"])[0]
        audio_ctx = int((q.get("ac") or ["0"])[0])
        greedy = (q.get("greedy") or ["1"])[0] != "0"

        try:
            result = transcribe(body, model, audio_ctx, greedy)
            self._send(200, json.dumps(result), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": repr(e)}), "application/json")


if __name__ == "__main__":
    print("voxterm: whisper=%s threads=%d" % (WHISPER, THREADS), flush=True)
    for k, v in MODELS.items():
        print("  model %-16s %s" % (k, "ok" if os.path.exists(v) else "MISSING"), flush=True)
    print("listening on http://%s:%d" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
