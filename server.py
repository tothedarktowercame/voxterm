#!/usr/bin/env python3
"""voxterm - push-to-talk voice terminal.

Browser captures mic -> 16 kHz mono WAV -> POST here -> whisper.cpp -> text back.

Stdlib only except for the optional instant-reply path, which imports the
anthropic SDK lazily — run under .venv/bin/python for that; everything else
works without it. Binds to loopback by default: reach it from the phone with an
ssh tunnel (see README), which also satisfies the browser's secure-context
requirement for getUserMedia without any TLS setup.
"""
import json
import base64
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
                  "elisp, Emacs, nREPL, futon3c, voxterm, tmux. "
                  "Bell Codex. Ask Codex. Codex agents. Zai runs GLM. "
                  "Use Opus. Switch to Opus. Opus and Fable. Haiku and Sonnet.")
PROMPT = os.environ.get("VOXTERM_PROMPT", DEFAULT_PROMPT)

# Backstop for residue the prompt doesn't catch. Keep this list *small* and only
# unambiguous: "call" and "Clojure" are also common mishearings of "Claude" but
# are real words here, so they must not be substituted.
FIXUPS = [
    (re.compile(r"\bquad\b", re.I), "Claude"),
    (re.compile(r"\b(?:codecs|kodak)\b", re.I), "Codex"),
    # Seeding the prompt was not enough for Joe's pronunciation: still "OPE".
    # Safe — "ope" is archaic-poetic, and \b protects open/hope/scope/rope.
    (re.compile(r"\bopes?\b", re.I), "Opus"),
    # Variants observed: "xi" (Joe), "Xai" and "Zaai" (TTS loop — the spelling
    # shifts whenever the decoder prompt changes, so match a family, not a word).
    # Drop the xai alternative if xAI the company ever comes up in dictation.
    (re.compile(r"\b(?:x[ia]i?|zaa+i|z\.ai)\b", re.I), "Zai"),
    (re.compile(r"\bfuton\s*3\s*c\b", re.I), "futon3c"),
    (re.compile(r"\bem\s*axe?\b", re.I), "Emacs"),
]


def apply_fixups(text):
    for pattern, replacement in FIXUPS:
        text = pattern.sub(replacement, text)
    return text


PROMPT_WORDS = frozenset(w.lower().strip(".,") for w in PROMPT.split())


def strip_prompt_echo(text, max_words=3):
    """Drop a short leading sentence made only of prompt vocabulary.

    On a noise burst before the first real word, the decoder echoes the
    prompt: "Claude Codex. And our own development as people..." (turbo,
    2026-08-24). Real dictation does not open with a <=3-word sentence built
    solely from the vocabulary list, so such a sentence is treated as echo.
    Applied repeatedly, so a whole segment of echo becomes empty.
    Returns (text, stripped?)."""
    stripped = False
    while True:
        m = re.match(r"\s*([^.!?]+)[.!?]+\s*", text)
        if not m:
            break
        words = m.group(1).split()
        if not words or len(words) > max_words:
            break
        if not all(w.lower().strip(".,;:'\"") in PROMPT_WORDS for w in words):
            break
        text = text[m.end():]
        stripped = True
    return text.strip(), stripped


def collapse_repeats(text, min_run=3, max_n=8):
    """Collapse a run of >= MIN_RUN identical consecutive n-grams to one.

    trim_stutter only looks at the tail; a decoder loop in the *body* — 20 s of
    "per the main title, " x36 from large-v3-turbo on 2026-08-24 — sails past
    it. Nobody dictates the same phrase three times running, so a run of three
    is a loop, whatever its length. Shortest n-gram first: longest-first sees
    36 x "per the main title," as 18 x an 8-gram and leaves a pair behind.
    Returns (text, collapsed?)."""
    words = text.split()
    key = [w.lower().strip(".,!?;:\u2014-") for w in words]
    changed = False
    for n in range(1, max_n + 1):
        i = 0
        out_w, out_k = [], []
        while i < len(words):
            run = 1
            while (i + (run + 1) * n <= len(words)
                   and key[i + run * n:i + (run + 1) * n] == key[i:i + n]):
                run += 1
            if run >= min_run:
                out_w.extend(words[i:i + n]); out_k.extend(key[i:i + n])
                i += run * n
                changed = True
            else:
                out_w.append(words[i]); out_k.append(key[i])
                i += 1
        words, key = out_w, out_k
    # Whole-passage duplication (66 words twice, turbo at a tight context)
    # is beyond any n-gram window: collapse while the text is k identical halves.
    while len(words) >= 8 and len(words) % 2 == 0 and key[:len(key) // 2] == key[len(key) // 2:]:
        words, key = words[:len(words) // 2], key[:len(key) // 2]
        changed = True
    return " ".join(words), changed


def trim_stutter(text):
    """Drop a degenerate repeating tail. Returns (text, trimmed?).

    whisper can fall into a decoder loop and emit something like
    "...both through both through through both through through". Observed in
    the wild, and not reproducible on demand, so it is handled after the fact
    rather than tuned away. The signature is lexical: a real clause keeps
    introducing new words, a loop stops. Beam search makes it rarer; this
    catches what still gets through.
    """
    words = text.split()
    if len(words) < 8:
        return text, False
    bare = [w.lower().strip(".,!?;:—-") for w in words]
    for k in range(min(16, len(words)), 5, -1):
        tail = bare[-k:]
        if len(set(tail)) * 2 <= k:          # under half the words are distinct
            seen, keep = set(), []
            for word, low in zip(words[-k:], tail):
                if low in seen:
                    break
                seen.add(low)
                keep.append(word)
            return " ".join(words[:-k] + keep).strip(), True
    return text, False

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
VOICES_DIR = os.path.join(TTS_DIR, "voices")
VOICE = os.environ.get("VOXTERM_VOICE", "en_GB-semaine-medium")
# en_GB-semaine is multi-speaker: 0 prudence, 1 spike, 2 obadiah, 3 poppy.
SPEAKER = os.environ.get("VOXTERM_SPEAKER", "2")
# Phoneme duration multiplier: < 1 speaks faster. The voice ships at 1.0, which
# is a slow, deliberate read — fine for an audiobook, draggy for a REPL.
LENGTH_SCALE = os.environ.get("VOXTERM_LENGTH_SCALE", "0.85")
# The commentator speaks in a different voice from the agent. A fast guess and a
# verified finding will sometimes disagree; you should never have to work out
# which one you just heard.
INSTANT_SPEAKER = os.environ.get("VOXTERM_INSTANT_SPEAKER", "3")
MAX_SPEAK_CHARS = 600


def voice_path(name):
    """Resolve a voice name to its .onnx, refusing anything outside voices/."""
    path = os.path.realpath(os.path.join(VOICES_DIR, (name or VOICE) + ".onnx"))
    if not path.startswith(os.path.realpath(VOICES_DIR) + os.sep):
        raise ValueError("voice outside voices/")
    if not os.path.exists(path):
        raise ValueError("no such voice: %s" % name)
    return path


def list_voices():
    return sorted(f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx"))

# Queue of agent text waiting to be spoken. Emacs enqueues (POST /say); the page
# drains it (GET /say/next) because the box has no speaker — the phone does.
_say_lock = threading.Lock()
_say_queue = []
MAX_SAY_QUEUE = 8
# Speech older than this is dropped at dequeue: the page may have been away for
# hours while Emacs kept enqueueing, and a backlog read out in a burst is noise.
SAY_TTL_S = float(os.environ.get("VOXTERM_SAY_TTL_S", "90"))

# Paragraphs that open with one of these are skipped: unspeakable, and the
# buffer already shows them.
_UNSPEAKABLE = re.compile(r"^\s*(```|~~~|\||#{1,6}\s|>\s)")


# --- instant reply -----------------------------------------------------------
# A commentator, not a worker: it reads the utterance and says what it
# understood, while the real turn goes to the agent in the REPL untouched. Its
# output is non-load-bearing, same as the audio channel it feeds.
COMMENTATOR_MODEL = os.environ.get("VOXTERM_COMMENTATOR_MODEL", "claude-opus-5")

# It thinks, it does not confirm. The line that matters is not "opinions vs no
# opinions" — it is reasoning about the problem (judged on its merits, and
# visibly checkable against the buffer) versus claiming agency over work it is
# not doing (a claim the user cannot check and has no reason to doubt).
COMMENTATOR_SYSTEM = """\
You are the spoken channel of a voice terminal: the user's thinking partner
while a separate, more capable agent does the actual work.

Facts about this surface:

- The user dictated the message; whisper small.en transcribed it, so identifiers
  and proper nouns may be mangled. If a word is clearly wrong, take what was
  obviously meant and carry on — do not stop to query it.
- Your reply is synthesised to speech and heard, not read. Code, paths, symbols
  and punctuation-heavy text do not survive being spoken.
- A more capable agent is already working on this in an Emacs buffer the user is
  looking at. You are not doing that work, and the user can see what it does.

Say the most useful thing you can right now, from what you already know: the
likely cause, the thing worth checking first, the caveat that will bite later,
or a direct answer if it is a question you can answer. Be specific to what was
actually said — a generic remark is worse than silence.

You are reasoning, not reporting. Never claim to have looked at anything, and
never say what the agent is going to do; you do not know, and the user cannot
check that claim the way they can check an idea. Where you are unsure, say so in
a few words and commit to a view anyway — a useful opinion that turns out wrong
costs nothing here, because the buffer on screen carries the truth.

Do not include internal or system XML tags in your response. One or two spoken
sentences, in plain language a person can follow by ear."""


CONTEXT_CHARS = int(os.environ.get("VOXTERM_CONTEXT_CHARS", "6000"))
KEY_FILE = os.path.expanduser(os.environ.get("VOXTERM_KEY_FILE", "~/.anthropic-key"))


def read_api_key():
    """ANTHROPIC_API_KEY, else the key file. Returns None if neither exists.

    Deliberately NOT written back into os.environ: this process spawns
    whisper-cli, piper and emacsclient, and children inherit the environment.
    Passing it to the client directly keeps the key out of those processes.
    """
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def fetch_context():
    """Tail of the Emacs conversation buffer, or '' if unavailable.

    Read-only and best-effort: with no context the commentator still works,
    it is just reasoning cold.
    """
    if CONTEXT_CHARS <= 0:
        return ""
    expr = ('(progn (unless (fboundp (quote voxterm-context)) (load %s t t))'
            ' (voxterm-context %d))' % (elisp_string(ELISP), CONTEXT_CHARS))
    try:
        proc = subprocess.run(["emacsclient", "-s", EMACS_SOCKET, "-e", expr],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return ""
        out = proc.stdout.strip()
        if out.startswith('"') and out.endswith('"'):
            out = out[1:-1]
        return base64.b64decode(out).decode("utf-8", "replace") if out else ""
    except Exception:
        return ""


# cli  — `claude -p`, billed to the Claude subscription. Slower (~4-5 s: process
#        startup dominates) but needs no API credits.
# api   — the SDK. Faster, but the API account is funded separately from a
#         Claude subscription and needs its own credits.
BACKEND = os.environ.get("VOXTERM_COMMENTATOR_BACKEND", "api")
CLI_MODEL = os.environ.get("VOXTERM_CLI_MODEL", "opus")
# Neutral cwd on purpose: run from ~/code and `claude -p` would inherit the
# futon3c handoff protocol from ~/code/CLAUDE.md, which has nothing to do with
# being a spoken commentator.
CLI_CWD = os.environ.get("VOXTERM_CLI_CWD", "/tmp")
NO_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"


def commentate_cli(prompt):
    """One-shot `claude -p`. Stateless: it represents no registered agent."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", CLI_MODEL,
         "--system-prompt", COMMENTATOR_SYSTEM,
         "--disallowed-tools", NO_TOOLS],
        capture_output=True, text=True, timeout=90, cwd=CLI_CWD)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "claude -p failed").strip()[-300:])
    return proc.stdout.strip()


def commentate_api(prompt):
    import anthropic  # lazy: the rest of the server runs without the SDK
    key = read_api_key()
    if not key:
        raise RuntimeError("no API key: set ANTHROPIC_API_KEY or ~/.anthropic-key")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=COMMENTATOR_MODEL,
        max_tokens=200,
        system=COMMENTATOR_SYSTEM,
        # Latency is the entire point, so thinking is off. Accepted on Opus 5 at
        # effort high or below. The documented risk of disabling it — tool calls
        # emitted as plain text — cannot apply here because no tools are given;
        # the other, leaked thinking tags, is covered in the system prompt.
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("refusal")
    return " ".join(b.text.strip() for b in resp.content
                    if getattr(b, "type", None) == "text").strip()


def commentate(text):
    """Ask the fast model for its most useful immediate thought."""
    context = fetch_context()
    if context:
        prompt = ("Here is the tail of the session the user is looking at, so "
                  "you can see what has already been said and done:\n\n"
                  "<transcript>\n%s\n</transcript>\n\n"
                  "The user has just said: %s" % (context, text))
    else:
        prompt = text

    said = commentate_cli(prompt) if BACKEND == "cli" else commentate_api(prompt)
    # Same cleanup the queue gets: this is going straight to piper.
    return sanitize_for_speech(said) or said


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


FRAMES_PER_SEC = 1500 / 30.0     # whisper encoder: 1500 frames per 30 s window
FULL_CTX = 1500


def fit_audio_ctx(requested, duration_sec, margin=256):
    """Smallest context >= REQUESTED that covers DURATION_SEC, or 0 for full.

    MARGIN is ~5 s: with only 2.5 s (128) large-v3-turbo emitted a 20 s
    passage twice at -ac 1152 and once, correctly, at 1280 (2026-08-24)."""
    need = int(duration_sec * FRAMES_PER_SEC) + margin
    need = ((need + 63) // 64) * 64
    ctx = max(requested, need)
    return 0 if ctx >= FULL_CTX else ctx


# Keep the last N clips on disk so a bad transcript can be re-run offline
# with other settings — the only way to tell "whisper dropped it" from "the
# page never sent it". 0 disables.
KEEP_CLIPS = int(os.environ.get("VOXTERM_KEEP_CLIPS", "12"))
CLIP_DIR = os.environ.get("VOXTERM_CLIP_DIR", "/tmp/voxterm-clips")


def keep_clip(wav_bytes, model_key):
    if KEEP_CLIPS <= 0:
        return None
    try:
        os.makedirs(CLIP_DIR, exist_ok=True)
        name = "%s-%s.wav" % (time.strftime("%Y%m%d-%H%M%S"), model_key)
        with open(os.path.join(CLIP_DIR, name), "wb") as f:
            f.write(wav_bytes)
        old = sorted(n for n in os.listdir(CLIP_DIR) if n.endswith(".wav"))
        for n in old[:-KEEP_CLIPS]:
            os.unlink(os.path.join(CLIP_DIR, n))
        return name
    except OSError:
        return None


# Clips shorter than this go to SHORT_MODEL whatever the page asked for: they
# are the trigger word or a noise burst, and large-v3-turbo spends ~8 s on
# "Rocket." (prompt-induced temperature fallback) where small.en takes 0.6 s.
SHORT_SEC = float(os.environ.get("VOXTERM_SHORT_SEC", "2.5"))
SHORT_MODEL = os.environ.get("VOXTERM_SHORT_MODEL", "small.en")


def transcribe(wav_bytes, model_key="small.en", audio_ctx=0, greedy=True):
    clip = keep_clip(wav_bytes, model_key)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxterm-")
    os.write(fd, wav_bytes)
    os.close(fd)
    try:
        with wave.open(path) as w:
            duration = w.getnframes() / float(w.getframerate())
        if duration < SHORT_SEC and SHORT_MODEL in MODELS:
            model_key = SHORT_MODEL
        model = MODELS.get(model_key, MODELS["small.en"])

        cmd = [WHISPER, "-m", model, "-f", path, "-t", str(THREADS),
               "-nt", "-np", "-l", "en", "-sns"]
        if PROMPT:
            cmd += ["--prompt", PROMPT]
        # A clipped context also applies to large-v3-turbo now that it is
        # fitted to the clip (below): measured 2026-08-24, same text as full
        # context at 6 s and 20 s, 2-2.5x faster. The README's turbo loop was
        # a window shorter than the audio, which fit_audio_ctx rules out.
        # The encoder context is a window on the audio: 1500 frames = 30 s, so
        # -ac 512 sees ~10 s and everything after it is dropped or garbled
        # (measured 2026-08-24 on an 18 s clip: 21 words of nonsense vs 32
        # right at full context — "eating half of what I say"). Scale the
        # requested context up to cover the clip, with a margin; at the top
        # just use the full context.
        if audio_ctx:
            audio_ctx = fit_audio_ctx(audio_ctx, duration)
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
        text, echoed = strip_prompt_echo(text)
        text, collapsed = collapse_repeats(text)
        text, stuttered = trim_stutter(text)
        return {
            "text": text,
            "stutter_trimmed": stuttered or collapsed,
            "prompt_echo_stripped": echoed,
            "model": model_key,
            "audio_ctx": audio_ctx,
            "clip": clip,
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


def preview(text, sink=None):
    """Mirror the pending buffer into Emacs as an overlay. Best effort."""
    sink = sink or SINK
    if sink != "emacs":
        return {"ok": True, "sink": sink, "detail": "no preview for this sink"}
    expr = ('(progn (unless (fboundp (quote voxterm-preview)) (load %s t t))'
            ' (voxterm-preview %s))' % (elisp_string(ELISP), elisp_string(text)))
    try:
        p = subprocess.run(["emacsclient", "-s", EMACS_SOCKET, "-e", expr],
                           capture_output=True, text=True, timeout=5)
        return {"ok": p.returncode == 0, "detail": (p.stdout or p.stderr).strip()}
    except Exception as e:
        return {"ok": False, "detail": repr(e)}


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


def synthesize(text, voice=None, speaker=None, length_scale=None):
    """Render TEXT to a 22 kHz WAV with piper. Returns the bytes."""
    text = text.strip()[:MAX_SPEAK_CHARS]
    if not text:
        raise ValueError("empty text")
    model = voice_path(voice)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxterm-tts-")
    os.close(fd)
    try:
        # cwd matters: piper resolves its bundled espeak-ng data relative to
        # the install tree, not to the model path.
        cmd = [PIPER, "-m", model, "-c", model + ".json", "-f", path,
               "-s", str(speaker if speaker is not None else SPEAKER),
               "--length-scale",
               str(length_scale if length_scale is not None else LENGTH_SCALE)]
        proc = subprocess.run(
            cmd, input=text, capture_output=True, text=True, timeout=60, cwd=TTS_DIR)
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

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # CORS: local pages on other ports (e.g. the VSAT board on :4321)
        # POST audio here; loopback-only bind keeps this private anyway.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # A (re)loaded page wants live speech, not whatever accumulated
            # while nobody was listening.
            with _say_lock:
                _say_queue.clear()
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    # Always revalidate: a phone PWA that keeps a stale page
                    # runs stale trigger/buffer logic against a fixed server.
                    self._send(200, f.read(), "text/html; charset=utf-8",
                               extra={"Cache-Control": "no-cache"})
            except OSError as e:
                self._send(500, str(e), "text/plain")
        elif path == "/say/next":
            with _say_lock:
                now = time.time()
                _say_queue[:] = [i for i in _say_queue if now - i["ts"] <= SAY_TTL_S]
                item = _say_queue.pop(0) if _say_queue else None
                depth = len(_say_queue)
            if item:
                item = dict(item); item.pop("ts", None)
            body = {"text": None, "voice": None, "speaker": None,
                    "length_scale": None, "elapsed_ms": None, "kind": None,
                    "depth": depth}
            if item:
                body.update(item)
            self._send(200, json.dumps(body), "application/json")
        elif path == "/health":
            ok = os.path.exists(WHISPER)
            models = {k: os.path.exists(v) for k, v in MODELS.items()}
            self._send(200, json.dumps({"whisper": ok, "models": models,
                                        "threads": THREADS,
                                        "voice": VOICE, "speaker": SPEAKER,
                                        "length_scale": LENGTH_SCALE,
                                        "voices": list_voices()}),
                       "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/transcribe", "/route", "/speak", "/say",
                               "/commentate", "/preview"):
            self._send(404, "not found", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(413, json.dumps({"error": "bad length"}), "application/json")
            return

        body = self.rfile.read(length)
        q = parse_qs(parsed.query)

        if parsed.path == "/commentate":
            try:
                payload = json.loads(body.decode("utf-8"))
                started = time.monotonic()
                said = commentate((payload.get("text") or "").strip())
                self._send(200, json.dumps(
                    {"ok": True, "text": said,
                     "model": (CLI_MODEL + " (cli)" if BACKEND == "cli"
                               else COMMENTATOR_MODEL),
                     "speaker": INSTANT_SPEAKER,
                     "ms": int((time.monotonic() - started) * 1000)}),
                    "application/json")
            except Exception as e:
                # Never fatal: the coding path is untouched, so a failure here
                # just means a quiet turn.
                detail = repr(e)
                low = detail.lower()
                if "credit balance" in low or "billing" in low:
                    # API credits are billed separately from a Claude subscription;
                    # having Claude Code does not fund this.
                    hint = "API account has no credits — top up in Plans & Billing"
                elif "no api key" in low or "authentication" in low:
                    hint = "set ANTHROPIC_API_KEY or ~/.anthropic-key"
                elif "rate_limit" in low or "429" in low:
                    hint = "rate limited — try again shortly"
                elif "overloaded" in low or "529" in low:
                    hint = "API overloaded — try again shortly"
                else:
                    hint = ""
                self._send(200, json.dumps({"ok": False, "detail": detail[:300],
                                            "hint": hint}), "application/json")
            return

        if parsed.path == "/say":
            try:
                payload = json.loads(body.decode("utf-8"))
                spoken = sanitize_for_speech((payload.get("text") or "").strip())
                if not spoken:
                    self._send(200, json.dumps({"ok": True, "queued": False,
                                                "reason": "unspeakable"}),
                               "application/json")
                    return
                item = {"text": spoken,
                        "voice": payload.get("voice"),
                        "speaker": payload.get("speaker"),
                        "length_scale": payload.get("length_scale"),
                        "elapsed_ms": payload.get("elapsed_ms"),
                        "kind": payload.get("kind"),
                        "ts": time.time()}
                with _say_lock:
                    # Stale speech is worse than dropped speech — keep it shallow.
                    if len(_say_queue) >= MAX_SAY_QUEUE:
                        _say_queue.pop(0)
                    _say_queue.append(item)
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
                wav = synthesize(payload.get("text", ""),
                                 payload.get("voice"), payload.get("speaker"),
                                 payload.get("length_scale"))
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

        if parsed.path == "/preview":
            # The phone's pending buffer, mirrored as ghost text in Emacs so
            # the user sees what "rocket" will send. Empty text clears it.
            try:
                payload = json.loads(body.decode("utf-8"))
                self._send(200, json.dumps(preview(payload.get("text", ""),
                                                   payload.get("sink"))),
                           "application/json")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "detail": repr(e)}),
                           "application/json")
            return

        if parsed.path == "/route":
            try:
                payload = json.loads(body.decode("utf-8"))
                print("route: sink=%s submit=%s text=%r" % (
                    payload.get("sink"), payload.get("submit"),
                    (payload.get("text") or "")[:200]), flush=True)
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
            # Journal the text: the page decides trigger/buffering on it, and
            # leaks are impossible to diagnose from status codes alone.
            print("transcribe: %s %.1fs %dms ctx=%s clip=%s text=%r" % (
                result.get("model", model), result.get("audio_sec") or 0, result.get("infer_ms") or 0,
                result.get("audio_ctx"), result.get("clip"),
                (result.get("text") or "")[:200]), flush=True)
            self._send(200, json.dumps(result), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": repr(e)}), "application/json")


if __name__ == "__main__":
    print("voxterm: whisper=%s threads=%d" % (WHISPER, THREADS), flush=True)
    for k, v in MODELS.items():
        print("  model %-16s %s" % (k, "ok" if os.path.exists(v) else "MISSING"), flush=True)
    print("listening on http://%s:%d" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
