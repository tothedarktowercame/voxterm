#!/usr/bin/env python3
"""voxterm - push-to-talk voice terminal.

Browser captures mic -> 16 kHz mono WAV -> POST here -> whisper.cpp -> text back.

Stdlib only except for the optional instant-reply path, which imports the
anthropic SDK lazily — run under .venv/bin/python for that; everything else
works without it. Binds to loopback by default: reach it from the phone with an
ssh tunnel (see README), which also satisfies the browser's secure-context
requirement for getUserMedia without any TLS setup.
"""
import collections
import glob
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
from urllib.request import Request, urlopen

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

WM_RUN_ROOT = os.path.expanduser(os.environ.get(
    "VOXTERM_WM_RUN_ROOT",
    "~/code/futon2/holes/labs/wm-contract/runs/RUN4-preparation-2026-09-10"))
WM_RUN_STATUS_FILE = os.environ.get("VOXTERM_WM_RUN_STATUS_FILE", "run-visibility.json")
WM_STALE_S = int(os.environ.get("VOXTERM_WM_STALE_S", "900"))

def _wm_age(iso):
    try:
        import datetime
        stamp = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        delta = time.time() - stamp.timestamp()
        # A small allowance avoids rejecting ordinary writer/reader clock
        # jitter. A record clearly from the future is not fresh evidence.
        return None if delta < -5 else max(0, int(delta))
    except (TypeError, ValueError, OverflowError):
        return None

def wm_run_status():
    """Fail-closed view of enacted evidence; never consults READY or Agency."""
    path = os.path.join(WM_RUN_ROOT, WM_RUN_STATUS_FILE)
    prep = any(os.path.isfile(os.path.join(WM_RUN_ROOT, n))
               for n in ("PREPARATION.md", "SERIES.edn"))
    if not os.path.isfile(path):
        return {"ok": True, "state": "absent", "run_evidence": False, "message": "no run evidence", "preparation_evidence": prep, "trial_detail": "absent", "source": path}
    try:
        with open(path, encoding="utf-8") as h: doc = json.load(h)
    except (OSError, ValueError) as exc:
        return {"ok": False, "state": "invalid", "run_evidence": False, "message": "run evidence malformed", "error": str(exc), "preparation_evidence": prep, "source": path}
    if not isinstance(doc, dict):
        return {"ok": False, "state": "invalid", "run_evidence": False,
                "message": "run evidence malformed", "preparation_evidence": prep,
                "source": path}
    stages = {"planned", "dispatched", "working", "review", "blocked",
              "failed", "complete", "accepted"}
    results = {"pending", "passed", "failed", "blocked"}
    age = _wm_age(doc.get("updated_at"))
    valid = (doc.get("schema") == "wm/run-visibility-v1"
             and isinstance(doc.get("run_id"), str) and bool(doc.get("run_id"))
             and isinstance(doc.get("stage"), str) and doc.get("stage") in stages
             and isinstance(doc.get("result"), str) and doc.get("result") in results
             and isinstance(doc.get("trials"), list) and bool(doc.get("trials"))
             and age is not None)
    trials = []
    if valid:
        for raw in doc["trials"]:
            tage = _wm_age(raw.get("updated_at")) if isinstance(raw, dict) else None
            if not (isinstance(raw, dict)
                    and isinstance(raw.get("trial_id"), str) and raw.get("trial_id")
                    and isinstance(raw.get("stage"), str) and raw.get("stage") in stages
                    and isinstance(raw.get("result"), str) and raw.get("result") in results
                    and tage is not None):
                valid = False
                break
            t = {k: raw.get(k) for k in
                 ("trial_id", "stage", "worker", "reviewer", "result",
                  "blocked_reason", "updated_at")}
            t.update(activity_age_s=tage, fresh=tage <= WM_STALE_S)
            trials.append(t)
    if not valid:
        return {"ok": False, "state": "invalid", "run_evidence": False, "message": "run evidence malformed", "preparation_evidence": prep, "source": path}
    stale = age > WM_STALE_S or any(not t["fresh"] for t in trials)
    return {"ok": True, "state": "stale" if stale else doc["stage"], "run_evidence": True, "run_id": doc["run_id"], "stage": doc["stage"], "worker": doc.get("worker"), "reviewer": doc.get("reviewer"), "result": doc.get("result", "pending"), "blocked_reason": doc.get("blocked_reason"), "updated_at": doc["updated_at"], "activity_age_s": age, "fresh": not stale, "trials": trials, "preparation_evidence": prep, "source": path}


def env_list(name, default):
    """Read a comma-separated VOXTERM setting, preserving configured order."""
    return [item.strip() for item in os.environ.get(name, default).split(",")
            if item.strip()]


CODEX_MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def codex_models(limit=None):
    """Model slugs the installed Codex CLI is currently offering.

    Codex slugs turn over fast (gpt-5.6-sol arrived after gpt-5.5), and the CLI
    already refreshes this cache for its own picker, so reading it is what keeps
    the buttons current without a hardcoded list drifting out of date. The
    literal is only for a box that has never run codex.
    """
    try:
        with open(CODEX_MODELS_CACHE) as handle:
            models = json.load(handle)["models"]
        listed = [m for m in models
                  if m.get("visibility") == "list" and m.get("supported_in_api")
                  and str(m.get("slug", "")).startswith("gpt-")]
        listed.sort(key=lambda m: m.get("priority", 1 << 30))
        slugs = [m["slug"] for m in listed[:limit]]
    except Exception:
        slugs = []
    return slugs or ["gpt-5.6-sol"]


# Model signatures for chips whose registration declared none. Both runtimes'
# session logs record the model for that specific seat.
_SESSION_MODEL_CACHE = {}   # sid -> (path, mtime, model)
CODEX_SESSIONS_ROOT = os.path.expanduser(os.environ.get(
    "VOXTERM_CODEX_SESSIONS_ROOT", "~/.codex/sessions"))
_CODEX_SESSION_MODEL_CACHE = {}  # sid -> (path, mtime_ns, size, model)


def claude_session_model(sid):
    """The model this seat's session last ran, from its .jsonl tail."""
    if not sid:
        return None
    cached = _SESSION_MODEL_CACHE.get(sid)
    try:
        path = (cached and cached[0]) or \
            glob.glob(os.path.expanduser(
                "~/.claude/projects/*/%s.jsonl" % sid))[0]
        mtime = os.path.getmtime(path)
        if cached and cached[1] == mtime:
            return cached[2]
        with open(path, "rb") as handle:
            handle.seek(max(0, os.path.getsize(path) - 262144))
            tail = handle.read().decode("utf-8", "replace")
        hits = re.findall(r'"model"\s*:\s*"(claude-[^"]+)"', tail)
        model = hits[-1] if hits else None
        _SESSION_MODEL_CACHE[sid] = (path, mtime, model)
        return model
    except (IndexError, OSError):
        return None


def codex_session_model(sid):
    """Latest turn-context model from this Codex session's rollout log."""
    if not sid:
        return None
    cached = _CODEX_SESSION_MODEL_CACHE.get(sid)
    try:
        path = (cached and cached[0]) or glob.glob(os.path.join(
            CODEX_SESSIONS_ROOT, "*", "*", "*", "rollout-*-%s.jsonl" % sid))[0]
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
        if cached and cached[:3] == key:
            return cached[3]
        with open(path, "rb") as handle:
            start = max(0, stat.st_size - 524288)
            handle.seek(start)
            tail = handle.read()
        lines = tail.splitlines()
        if start and lines:
            lines = lines[1:]  # first record may begin before the bounded tail
        model = None
        for line in lines:
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue  # includes a partially written trailing record
            if record.get("type") == "turn_context":
                candidate = (record.get("payload") or {}).get("model")
                if isinstance(candidate, str) and candidate:
                    model = candidate
        _CODEX_SESSION_MODEL_CACHE[sid] = key + (model,)
        return model
    except (IndexError, OSError):
        return None


AGENT_RUNTIMES = {
    "claude": {"label": "Claude", "model-prefix": "claude-",
               "models": env_list("VOXTERM_CLAUDE_MODELS",
                                  "claude-opus-5,claude-fable-5,"
                                  "claude-sonnet-5,claude-haiku-4-5-20251001"),
               "attach": "claude-repl-attach-agent"},
    "codex": {"label": "Codex", "model-prefix": "gpt-",
              "models": env_list("VOXTERM_CODEX_MODELS",
                                 ",".join(codex_models())),
              "attach": "codex-repl-attach-agent"},
    "zai": {"label": "Z.AI", "model-prefix": "glm-",
            "models": env_list("VOXTERM_ZAI_MODELS", "glm-5.2"),
            "attach": "zai-repl-attach-agent"},
}

def runtime_choices():
    """Refresh CLI model discovery when the picker opens, including new models."""
    choices = []
    for runtime, spec in AGENT_RUNTIMES.items():
        models = (env_list("VOXTERM_CODEX_MODELS", ",".join(codex_models()))
                  if runtime == "codex" else spec["models"])
        choices.append({"type": runtime, "label": spec["label"],
                        "models": models, "model-prefix": spec["model-prefix"]})
    return choices


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


def collapse_repeats(text, min_run=3, max_n=None):
    """Collapse a run of >= MIN_RUN identical consecutive n-grams to one.

    trim_stutter only looks at the tail; a decoder loop in the *body* — 20 s of
    "per the main title, " x36 from large-v3-turbo on 2026-08-24 — sails past
    it. Nobody dictates the same phrase three times running, so a run of three
    is a loop, whatever its length. Shortest n-gram first: longest-first sees
    36 x "per the main title," as 18 x an 8-gram and leaves a pair behind.

    MAX_N defaults to the longest phrase the text could possibly repeat, not a
    constant: a run of MIN_RUN copies needs MIN_RUN * n words, so nothing above
    len // MIN_RUN can match, and testing further is wasted. The old cap of 8
    let a 17-word unit repeat six times — 684 characters out of 11 s of speech,
    which had to be binned by hand (2026-08-29). Scaling with the text costs
    nothing: the scan is O(max_n * len) and runs once per utterance.
    Returns (text, collapsed?)."""
    words = text.split()
    key = [w.lower().strip(".,!?;:\u2014-") for w in words]
    if max_n is None:
        max_n = max(8, len(words) // min_run)
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


SEGMENT_RE = re.compile(
    r"^\[(\d\d):(\d\d):(\d\d\.\d+)\s*-->\s*(\d\d):(\d\d):(\d\d\.\d+)\]\s*(.*)$")


def parse_segments(stdout):
    """Parse whisper-cli's timestamped output into (start, end, text) tuples.

    Requires the run to keep timestamps (no -nt). Lines that do not match the
    timestamp form are appended to the previous segment, which is what a
    wrapped segment looks like."""
    segs = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = SEGMENT_RE.match(line)
        if m:
            t0 = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            t1 = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
            segs.append([t0, t1, m.group(7).strip()])
        elif segs:
            segs[-1][2] = (segs[-1][2] + " " + line).strip()
    return [tuple(s) for s in segs]


def _norm_words(text):
    return [w for w in (w.lower().strip(".,!?;:'\"\u2014-") for w in text.split()) if w]


# Nobody speaks at 12 words a second. Well under the ~2-3 words/s of ordinary
# speech, so a segment has to be wildly impossible before it is dropped.
MIN_SEC_PER_WORD = 0.08


def drop_looped_segments(segs, duration):
    """Drop segments claiming more words than their audio can hold.

    Returns (segments, dropped). whisper's repeat loop has a *structural*
    signature that the joined text hides. On a 12.0 s clip at -ac 896
    (2026-08-30, clip 20260830-142539) turbo emitted the same passage five
    times:

        [00:00:00.000 --> 00:00:11.160]  Can you explain ... I'm surprised.
        [00:00:11.160 --> 00:00:11.160]  Can you explain ... I'm surprised.   x3
        [00:00:11.160 --> 00:00:15.160]  Can you explain to me why ... I'm surprised.

    Three copies span zero seconds. The fourth claims 4 s starting at 11.160,
    but the clip ends at 11.97, so 13 words rest on 0.81 s of audio. Only the
    first copy is anchored to speech that exists. -nt throws all of that away
    and leaves the string heuristics below a problem they cannot solve: the
    copies are neither adjacent n-grams nor identical halves, so
    collapse_repeats never fires and the passage reaches the pending buffer
    twice — which is what Joe was seeing in dispatched turns.

    So judge each segment on audio rather than on wording: clip it to the real
    extent of the file and require MIN_SEC_PER_WORD of that for every word it
    claims. A genuinely repeated phrase is unaffected, because it is backed by
    the seconds it took to say. The first segment is always kept, so a clip
    whose one segment is degenerate still yields its text; a loop that stays
    inside its own segment is still collapse_repeats' problem."""
    kept, dropped = [], 0
    for i, (t0, t1, text) in enumerate(segs):
        words = _norm_words(text)
        if i and words:
            real = max(0.0, min(t1, duration) - min(t0, duration))
            if real < MIN_SEC_PER_WORD * len(words):
                dropped += 1
                continue
        kept.append((t0, t1, text))
    return kept, dropped


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


def fit_audio_ctx(requested, duration_sec, margin=512):
    """Smallest context >= REQUESTED that covers DURATION_SEC, or 0 for full.

    MARGIN is the padding left after the speech inside the encoder window, and
    too little of it is what makes turbo repeat itself. When the window ends
    shortly after the last word, whisper seeks past that word, finds a second
    of near-silence, and re-decodes it primed by everything it has already
    said — which comes back as the whole passage again (see
    drop_looped_segments). Swept on the 12.0 s clip 20260830-142539: 83 words
    (the passage twice) at -ac 832/896/960, 35 words (correct) at 1024 and
    above. 896 is exactly where margin=256 put it. 512 puts it at 1152, which
    still beats the full context on time (6.7 s vs 8.7 s), so the speed the
    clipped context is here for survives the change.

    This is a band measured on one clip, not a proof; drop_looped_segments is
    what makes a loop harmless when the band moves."""
    need = int(duration_sec * FRAMES_PER_SEC) + margin
    need = ((need + 63) // 64) * 64
    ctx = max(requested, need)
    return 0 if ctx >= FULL_CTX else ctx


# Keep the last N clips on disk so a bad transcript can be re-run offline
# with other settings — the only way to tell "whisper dropped it" from "the
# page never sent it". 0 disables.
# 12 was too few to debug with: the clips behind a transcript Joe queried at
# 14:27 had already been rotated out by 14:35 (2026-08-30).
KEEP_CLIPS = int(os.environ.get("VOXTERM_KEEP_CLIPS", "48"))
# "Recently active" for the voxterm agent chips: long enough to still show an agent
# you were talking to a few minutes ago, short enough that the row stays short.
RECENT_SEC = int(os.environ.get("VOXTERM_RECENT_SEC", "1800"))
MAX_ACTIVE = int(os.environ.get("VOXTERM_MAX_ACTIVE", "8"))
# Job states that mean the work is over; everything else counts as live.
TERMINAL_STATES = ("done", "failed", "cancelled", "error", "timeout")
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
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxterm-")
    os.write(fd, wav_bytes)
    os.close(fd)
    try:
        with wave.open(path) as w:
            duration = w.getnframes() / float(w.getframerate())
        if duration < SHORT_SEC and SHORT_MODEL in MODELS:
            model_key = SHORT_MODEL
        model = MODELS.get(model_key, MODELS["small.en"])
        # Named after the model that ran, not the one the page asked for: every
        # clip on disk claimed large-v3-turbo, including the ones small.en
        # actually transcribed, so re-running one offline reproduced nothing.
        clip = keep_clip(wav_bytes, model_key)

        # Timestamps stay ON (no -nt): drop_looped_segments needs the segment
        # boundaries to tell a repeat loop from speech, and they are stripped
        # again before the text is returned.
        cmd = [WHISPER, "-m", model, "-f", path, "-t", str(THREADS),
               "-np", "-l", "en", "-sns"]
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

        segs, looped = drop_looped_segments(parse_segments(proc.stdout), duration)
        text = " ".join(s[2] for s in segs if s[2])
        text = apply_fixups(text)
        text, echoed = strip_prompt_echo(text)
        text, collapsed = collapse_repeats(text)
        text, stuttered = trim_stutter(text)
        return {
            "text": text,
            "looped_segments": looped,
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


# Agents voxterm has actually been pointed at, newest last. The Agency cannot
# supply this: an agent the operator talks to directly raises no invoke job, and
# `last-active' is rewritten wholesale by the restore sweep (80 agents stamped with
# one timestamp, 2026-08-29). So claude-7 vanished from the row the moment the
# cursor moved elsewhere, despite being the agent under discussion. voxterm is the
# only thing that knows where it has been aiming, so it keeps the list.
_recent_targets = collections.OrderedDict()


def note_target(agent):
    """Record AGENT as somewhere dictation has recently been aimed."""
    if not agent:
        return
    _recent_targets.pop(agent, None)
    _recent_targets[agent] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    while len(_recent_targets) > MAX_ACTIVE:
        _recent_targets.popitem(last=False)


def current_target():
    """The agent whose buffer a dictation would land in right now, or None.

    Read straight from Emacs rather than tracked here: `voxterm-target-name' is
    already the one authority on that, and a second guess in this file would be a
    second thing to keep true. Best effort -- a target we cannot read is shown as
    no target, never as the wrong one."""
    if SINK != "emacs":
        return None
    expr = ('(progn (unless (fboundp (quote voxterm-target-name)) (load %s t t))'
            ' (format "%%s|%%s" (voxterm-target-name)'
            ' (if (voxterm-target-pinned-p) "pinned" "focus")))'
            % elisp_string(ELISP))
    try:
        p = subprocess.run(["emacsclient", "-s", EMACS_SOCKET, "-e", expr],
                           capture_output=True, text=True, timeout=3)
        if p.returncode != 0:
            return None
        raw = p.stdout.strip().strip('"')
    except Exception:
        return None
    name, _, mode = raw.partition("|")
    if not name or name.startswith("nil"):
        return None
    # "*claude-repl:claude-7*" -> "claude-7"; the fallback form carries a trailing
    # explanation and no agent, so it yields a buffer with no id, which is honest.
    agent = None
    core = name.strip("*")
    if name.startswith("*") and ":" in core and " " not in core:
        agent = core.split(":")[-1].strip("*")
    note_target(agent)
    return {"buffer": name, "agent": agent, "pinned": mode == "pinned"}


def set_target(agent, mode="pin"):
    """Point dictation at AGENT's live Emacs buffer, or unpin for None.

    MODE "focus" moves the cursor into the buffer and leaves voxterm following
    focus -- the first tap on a chip, and the weaker of the two claims. MODE
    "pin" makes the buffer follow the operator into every frame, which is the
    second tap.

    Agency supplies the runtime type; Emacs remains authoritative about whether
    the derived buffer actually exists. Every failure leaves the previous target
    untouched and is returned as data rather than escaping into the handler.
    """
    if SINK != "emacs":
        return {"ok": False, "reason": "Emacs is not the active sink"}
    if mode not in ("focus", "pin"):
        return {"ok": False, "reason": "unknown target mode %r" % mode}
    if agent is None:
        expr = ('(progn (unless (fboundp (quote voxterm-unpin)) (load %s t t))'
                ' (voxterm-unpin))' % elisp_string(ELISP))
    else:
        if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", agent):
            return {"ok": False, "reason": "invalid agent id"}
        try:
            with urlopen("http://127.0.0.1:7070/api/alpha/agents/" + agent,
                         timeout=2.5) as response:
                record = json.load(response)
            info = record.get("agent") or {}
            if not record.get("ok") or (info.get("id") or {}).get("id/value") != agent:
                return {"ok": False, "reason": "agent is not registered"}
            runtime = info.get("type")
        except Exception as e:
            return {"ok": False, "reason": "could not resolve agent: %s" % e}
        prefixes = {"claude": "claude-repl", "codex": "codex-repl",
                    "zai": "zai-repl"}
        prefix = prefixes.get(runtime)
        if not prefix:
            return {"ok": False,
                    "reason": "agent type %r has no Emacs buffer mapping" % runtime}
        buffer_name = "*%s:%s*" % (prefix, agent)
        command = "voxterm-focus" if mode == "focus" else "voxterm-pin"
        expr = ('(progn (unless (fboundp (quote %s)) (load %s t t))'
                ' (%s %s))' % (command, elisp_string(ELISP),
                               command, elisp_string(buffer_name)))
    try:
        p = subprocess.run(["emacsclient", "-s", EMACS_SOCKET, "-e", expr],
                           capture_output=True, text=True, timeout=3)
    except Exception as e:
        return {"ok": False, "reason": "Emacs target request failed: %s" % e}
    if p.returncode != 0:
        return {"ok": False,
                "reason": (p.stderr or p.stdout or "Emacs rejected target")[-300:].strip()}
    target = current_target()
    if agent is not None and (not target or target.get("agent") != agent
                              or target.get("pinned") != (mode == "pin")):
        return {"ok": False,
                "reason": "Emacs did not confirm the requested %s" % mode}
    return {"ok": True, "mode": mode, "target": target}


def create_agent_target(runtime, model):
    """Register a selected agent, attach its REPL, then pin dictation.

    Registration necessarily precedes the Emacs buffer.  If attachment fails,
    report the registered agent plainly but leave the existing pin untouched.
    """
    spec = AGENT_RUNTIMES.get(runtime)
    if spec is None:
        return {"ok": False, "step": "validate",
                "reason": "unsupported runtime %r; choose claude, codex, or zai"
                          % runtime}
    if not isinstance(model, str) or not model.strip():
        return {"ok": False, "step": "validate",
                "reason": "model must be a non-empty string"}
    model = model.strip()
    if not model.startswith(spec["model-prefix"]):
        return {"ok": False, "step": "validate",
                "reason": "model %r is incompatible with %s; expected prefix %s"
                          % (model, runtime, spec["model-prefix"])}
    if SINK != "emacs":
        return {"ok": False, "step": "validate",
                "reason": "Emacs is not the active sink"}
    previous_target = current_target()
    registration = {"type": runtime, "model": model, "cwd": "/home/joe/code"}
    payload = json.dumps(registration).encode()
    try:
        request = Request("http://127.0.0.1:7070/api/alpha/agents/auto",
                          data=payload, method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=4) as response:
            registered = json.load(response)
    except Exception as e:
        return {"ok": False, "step": "register",
                "reason": "Agency registration failed: %s" % e}
    agent = registered.get("agent-id") if isinstance(registered, dict) else None
    if (not isinstance(registered, dict) or not registered.get("ok")
            or not isinstance(agent, str) or not agent):
        return {"ok": False, "step": "register",
                "reason": (registered.get("message") if isinstance(registered, dict) else None)
                          or (registered.get("err") if isinstance(registered, dict) else None)
                          or "Agency did not return an agent id"}

    expr = "(%s %s)" % (spec["attach"], elisp_string(agent))
    try:
        attached = subprocess.run(["emacsclient", "-s", EMACS_SOCKET, "-e", expr],
                                  capture_output=True, text=True, timeout=4)
    except Exception as e:
        return {"ok": False, "step": "attach", "agent": agent,
                "reason": "agent registered, but Emacs attach failed: %s" % e}
    if attached.returncode != 0:
        detail = (attached.stderr or attached.stdout or "Emacs rejected attach")
        return {"ok": False, "step": "attach", "agent": agent,
                "reason": "agent registered, but Emacs attach failed: %s"
                          % detail[-300:].strip()}

    pinned = set_target(agent)
    if not pinned.get("ok"):
        # A failed confirmation can follow an Emacs command that did change the
        # pin. Restore what the operator had before this request (2026-08-29:
        # partial creation must not redirect the next dictated sentence).
        restore_agent = (previous_target.get("agent")
                         if previous_target and previous_target.get("pinned") else None)
        restored = set_target(restore_agent)
        restore_note = ""
        if not restored.get("ok"):
            restore_note = "; previous pin restoration also failed: %s" % (
                restored.get("reason") or "unknown failure")
        return {"ok": False, "step": "pin", "agent": agent,
                "reason": (pinned.get("reason") or "REPL attached, but pin failed")
                          + restore_note}
    return {"ok": True, "type": runtime, "model": model, "agent": agent,
            "target": pinned.get("target")}


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



def _agency_jvm_pid():
    """Pid of the process listening on :7070, or None."""
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                         timeout=2).stdout
    for line in out.splitlines():
        if ":7070 " in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _short_cmd(comm, args):
    """One phrase a human reads: what this process is, not its flags."""
    a = args.split()
    if not a:
        return comm
    base = os.path.basename(a[0])
    if base == "java" or base.startswith("clojure"):
        # `clojure -M -m ns` / `-M:test path` / a script path
        for i, tok in enumerate(a):
            if tok == "-m" and i + 1 < len(a):
                return "clojure -m " + a[i + 1]
        for tok in a[1:]:
            if tok.endswith((".clj", ".bb")):
                return "clojure " + os.path.basename(tok)
        return "java"
    if base == "bb":
        for tok in a[1:]:
            if tok.endswith(".bb"):
                return "bb " + os.path.basename(tok)
        return "bb"
    if base == "node" and any("codex" in t for t in a[:2]):
        return "codex " + (a[2] if len(a) > 2 else "")
    if base == "codex" or "codex-linux" in a[0]:
        return "codex (worker)"
    if base == "claude":
        return "claude seat"
    if base in ("bash", "sh") and len(a) > 2 and a[1] == "-c":
        body = " ".join(a[2:])
        body = re.sub(r"^source \S+ 2>/dev/null \|\| true &?\s*", "", body)
        # The Claude tool shell prefixes every command with a long snapshot
        # preamble; the command itself is at the END, so show the tail.
        if "shell-snapshots" in body or "\\builtin" in body:
            body = body.rstrip()
            body = body[-70:]
            return "bash \u2026" + body
        return ("bash: " + body[:60]) if body else "bash"
    m = re.search(r"([\w-]*-build-loop\.sh)", args)
    if m:
        return m.group(1)
    if base == "lake":
        return "lake " + " ".join(a[1:3])
    return " ".join([base] + [os.path.basename(t) for t in a[1:2]])[:60]


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")

# session id -> agent id, remembered across polls. The registry is the source of
# truth, but this endpoint re-reads it every 7s and one response that omits an
# agent (or omits its session-id while it re-registers) is enough to strand that
# agent's seat as "unattributed" for the frame. Session ids are immutable and
# never reassigned, so remembering a mapping can only ever add information.
_SESSION_OWNERS = {}


def _session_in(args):
    """A Claude seat is `claude --resume <sid>` and a resumed Codex is
    `codex resume <sid>`; the roster's `session-id` is the same string, which
    makes it the one deterministic key between a process and an agent."""
    m = _UUID.search(args or "")
    return m.group(0) if m else None


def _seat_shaped(proc):
    """A persistent agent seat, as opposed to a one-shot job the JVM spawned.
    Both seat kinds are named by _short_cmd: a Claude seat is any `claude ...`
    ("claude seat"), a Codex seat is `codex exec`. Codex's own workers are
    children of the seat, never direct JVM children, so they cannot reach the
    caller of this."""
    cmd = proc.get("cmd") or ""
    return cmd == "claude seat" or cmd.startswith("codex exec")


def agency_procs():
    """{ok, jvm, agents:[{id, pid, start, tree:[...]}], unmatched:[...]}."""
    jvm = _agency_jvm_pid()
    if not jvm:
        return {"ok": False, "error": "no process listens on :7070",
                "agents": [], "unmatched": []}
    ps = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,pcpu=,comm=,args="],
                        capture_output=True, text=True, timeout=3).stdout
    now = time.time()
    procs, kids = {}, {}
    for line in ps.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        pid, ppid, et, cpu, comm = (int(parts[0]), int(parts[1]),
                                    int(parts[2]), float(parts[3]), parts[4])
        args = parts[5] if len(parts) > 5 else ""
        procs[pid] = {"pid": pid, "ppid": ppid, "elapsed": et, "cpu": cpu,
                      "comm": comm, "cmd": _short_cmd(comm, args),
                      "start": now - et, "session": _session_in(args)}
        kids.setdefault(ppid, []).append(pid)

    def cwd_of(pid):
        try:
            return os.path.basename(os.readlink("/proc/%d/cwd" % pid)) or "/"
        except OSError:
            return ""

    def subtree(pid, depth=0, budget=[14]):
        node = dict(procs[pid])
        node["cwd"] = cwd_of(pid)
        node.pop("start", None)
        node.pop("session", None)
        node["children"] = []
        if depth < 5:
            for k in sorted(kids.get(pid, []), key=lambda k: -procs[k]["elapsed"]):
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                node["children"].append(subtree(k, depth + 1, budget))
        return node

    def epoch(when):
        try:
            return time.mktime(time.strptime(when[:19], "%Y-%m-%dT%H:%M:%S")) \
                - time.timezone
        except (TypeError, ValueError):
            return None

    # Two keys, in order. (1) session id: the child's cmdline carries it and
    # the roster has it -- exact. (2) start time, for a seat whose session is
    # new (no id on the cmdline yet): the child starts within seconds of the
    # agent's invoke or of its running job. Time alone misses Claude seats,
    # because `invoke-started-at` is rewritten every TURN while the process is
    # per SEAT (claude-1: process 17:20:41, invoke-started-at 17:25:58 on
    # 2026-09-01) -- which is what produced the "?" rows Joe asked about.
    by_session, starts, info, act_at, turn_start = {}, [], {}, {}, {}
    with urlopen("http://127.0.0.1:7070/api/alpha/agents", timeout=2.5) as r:
        for agent in (json.load(r).get("agents") or {}).values():
            ident = (agent.get("id") or {}).get("id/value")
            if not ident:
                continue
            info[ident] = (agent.get("status"), agent.get("invoke-activity"))
            act_at[ident] = epoch(agent.get("invoke-activity-at"))
            # Rewritten every TURN (not per seat), so for an agent that is
            # actually in a turn this is that turn's start -- the line between
            # work this turn spawned and work carried over from an earlier one.
            turn_start[ident] = epoch(agent.get("invoke-started-at"))
            if agent.get("session-id"):
                by_session[agent["session-id"]] = ident
            t = epoch(agent.get("invoke-started-at"))
            if t:
                starts.append((t, ident))
    # Agents with a live job. Necessary but NOT sufficient on its own: a seat
    # driven straight from the operator's REPL runs a turn without ever minting
    # an invoke job, so claude-4 mid-turn shows no job at all. Pair it with
    # recent reported activity, on the same 120s bound the chips use.
    # The roster `status` on these rows is intent, not liveness (2026-09-04),
    # so it cannot answer this: an idle-flagged seat may still be mid-turn, and
    # an invoking-flagged one may have finished.
    turn_agents = set()
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                     timeout=2.5) as r:
            for job in (json.load(r).get("jobs") or []):
                if job.get("state") in ("running", "queued"):
                    t = epoch(job.get("started-at") or job.get("created-at"))
                    if job.get("agent-id"):
                        turn_agents.add(job["agent-id"])
                        if t:
                            starts.append((t, job["agent-id"]))
    except Exception:
        pass

    # (4) a role-runner job (`apm-role-*`) makes the JVM spawn one short
    # process per command, directly, with no session and a cwd that need not
    # name the agent. If exactly ONE such job is running, its transient spawns
    # are its; if several are, a guess would be a fabrication, so they stay "?".
    role_agents = set()
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                     timeout=2.5) as r:
            for job in (json.load(r).get("jobs") or []):
                if (job.get("state") == "running"
                        and str(job.get("job-id", "")).startswith("apm-role")
                        and job.get("agent-id")):
                    role_agents.add(job["agent-id"])
    except Exception:
        pass
    sole_role = next(iter(role_agents)) if len(role_agents) == 1 else None

    def in_turn(ident):
        """Is this agent running a turn right now?

        The roster `status` cannot answer it -- that flag is intent, not
        liveness (2026-09-04). Two signals that are: a live invoke job, or
        activity reported within the chips' 120s window. An operator-driven
        seat has the second without the first; a bell-driven one usually has
        both.
        """
        if ident in turn_agents:
            return True
        t = act_at.get(ident)
        return bool(t and (time.time() - t) <= 120)

    def mark_carried(root, cutoff):
        """Flag descendants that predate CUTOFF; return whether any did.

        The root is the seat process itself, which predates every turn by
        construction, so it is never flagged -- only what runs beneath it. A
        two-second slack absorbs the skew between the roster's timestamp and
        `ps` elapsed seconds.
        """
        now = time.time()
        found = [False]

        def walk(node, inherited):
            for kid in node.get("children") or []:
                started = now - (kid.get("elapsed") or 0)
                # Inherit: a `sleep 10` freshly spawned INSIDE a carried-over
                # watch loop is part of that background job, not new turn work,
                # and labelling it otherwise splits one job across two readings.
                bg = inherited or cutoff is None or started < cutoff - 2
                if bg:
                    kid["background"] = True
                    found[0] = True
                walk(kid, bg)
        walk(root, False)
        return found[0]

    # Attribution for unmatched JVM children. A seat process persists long
    # after its job finished, so job created-at correlation decays as soon as
    # that job ages out of the endpoint's rolling 20-job window (claude-10's
    # seat, 2026-09-04: attributable at rehearsal, "?" again 30 minutes
    # later). The durable key is the REGISTRY: an agent's `registered-at` is
    # stamped once, never expires, and the seat process starts within seconds
    # of it. Job created-at and invoke-started-at stay as additional
    # candidates; for a non-seat child this claims likely ownership without
    # consuming the `used` set -- it names the seat, it does not assert a
    # running turn. Path (5) below does consume it, but only for a seat-shaped
    # child, where naming the seat and owning the name are the same claim.
    # (Claude
    # seats carry no session id in argv -- it arrives over stdin -- and
    # /proc/<pid>/environ, checked 2026-09-04, holds no UUID either.)
    attr_candidates = []
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/agents",
                     timeout=2.5) as r:
            for agent in (json.load(r).get("agents") or {}).values():
                t = epoch(agent.get("registered-at"))
                ident = (agent.get("id") or {}).get("id/value")
                if t and ident:
                    attr_candidates.append((t, ident))
    except Exception:
        pass
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                     timeout=2.5) as r:
            for job in (json.load(r).get("jobs") or []):
                t = epoch(job.get("created-at"))
                if t and job.get("agent-id"):
                    attr_candidates.append((t, job["agent-id"]))
    except Exception:
        pass
    for t, ident in starts:
        if (t, ident) not in attr_candidates:
            attr_candidates.append((t, ident))

    def known_agent(ident):
        """An id only if the registry still knows it.

        Naming a DEPARTED agent is worse than naming none. "Unattributed" is a
        gap and reads as one; an ex-agent is a fiction, and it sends someone
        looking for a process nobody is running. Both remaining attribution
        sources can serve one: the rolling job window outlives the roster, and
        the session cache below never expires.
        """
        return ident if ident in info else None

    def likely_owner(child_start):
        best = None
        for t, cand in attr_candidates:
            if not known_agent(cand):
                continue
            d = child_start - t
            if -5 <= d <= 20 and (best is None or abs(d) < best[0]):
                best = (abs(d), cand)
        return best[1] if best else None

    _SESSION_OWNERS.update(by_session)

    def owner_of_session(sid):
        """The agent a `--resume <sid>` seat belongs to, registry first.

        The cache exists so that a registry which briefly loses an agent, or
        omits its session-id while it re-registers, does not strand that seat
        as unattributed for the frame. Its premise is that session ids are
        immutable and never reassigned -- true of the SESSION, but not of who
        owns it. An agent id can be retired while its session is taken over by
        another, and then the cached name is simply wrong and never corrected.

        2026-09-07: the panel showed "claude-10" running
        /tmp/f10-unblock-watch.sh. claude-10 was in neither the roster nor the
        job window; the seat's `--resume b2de8138` session belongs to claude-1,
        which had taken it over. The process was real and correctly attributed
        to a live seat by every other path -- only the cached name was stale.

        So the cache may answer only for an agent the registry still knows.
        Otherwise fall through and let the other matchers speak, or leave the
        row unattributed, which is the honest answer.
        """
        return by_session.get(sid) or known_agent(_SESSION_OWNERS.get(sid))

    rows, unmatched, used = [], [], set()
    # The unattended build loop (futon2 wm-build-loop.sh) runs OUTSIDE the
    # Agency JVM -- a bash loop under nohup that spawns `claude -p` / `codex
    # exec` seats itself -- so it would be invisible to a JVM-rooted tree.
    # Show it as its own root, named for what it is.
    loop_pids = [pid for pid, pr in procs.items()
                 if pr["cmd"].endswith("-build-loop.sh")]
    # nohup's shell and the script's own shell both carry the script name; show
    # the topmost only, or one loop reads as two.
    for pid in sorted(loop_pids):
        if procs[pid]["ppid"] in loop_pids and \
           procs[procs[pid]["ppid"]]["cmd"] == procs[pid]["cmd"]:
            continue
        rows.append({"id": procs[pid]["cmd"][:-3], "status": "loop",
                     "activity": "building from the ledger",
                     "matched-by": "cmdline", "pid": pid,
                     "elapsed": procs[pid]["elapsed"], "tree": subtree(pid, 0, [16])})
    for child in sorted(kids.get(jvm, []), key=lambda k: procs[k]["start"]):
        c = procs[child]
        ident, how = None, None
        sid = c.get("session")
        # `--resume <sid>` in argv IS the identity: the registry maps that uuid
        # to exactly one agent, so a session hit is a fact, not a guess, and
        # must not be gated on `used`. When one agent legitimately has two live
        # seats (the previous turn's not yet reaped, the next turn's already
        # spawned) the gate sent the second one down the fuzzy paths into
        # `unmatched`, where a registered-at window days stale could not name it
        # either -- printing "unattributed seat" directly beside the session id
        # that identifies it (Joe, 2026-09-05). `used` still guards the
        # start-time path below, which is the one that can actually collide.
        if sid and owner_of_session(sid):
            ident, how = owner_of_session(sid), "session"
        else:
            best = None
            for t, cand in starts:
                if cand in used:
                    continue
                d = c["start"] - t          # a child starts AFTER its invoke
                if -3 <= d <= 20 and (best is None or abs(d) < best[0]):
                    best = (abs(d), cand)
            if best:
                ident, how = best[1], "start-time"
        if not ident:
            # (3) working directory. Role-runner jobs are spawned by the JVM
            # directly, one short bash per command, with no session and no
            # start time worth matching -- but their cwd names the agent
            # (`apm-frames/f78-b96A02-student` -> f78-student). Every '-'
            # token of the agent id must appear in the path.
            try:
                path = os.readlink("/proc/%d/cwd" % child).lower()
            except OSError:
                path = ""
            # A cwd says where work happens, not who is doing it, and the
            # two come apart the moment a job borrows another frame's
            # directory. On 2026-09-07 f188-student ran
            #   cd /tmp/f188-lean && sed -i ... Main.lean
            #   cd ~/code/apm-frames/f167-m99J04-student && lake env lean ...
            # -- using a frame CLOSED hours earlier as a Lean project root,
            # writing nothing into it -- and this path read the borrowed path
            # as ownership. The panel showed a live "f167-student" tree for a
            # dead frame, on a problem nobody had scheduled (Joe: "that should
            # never happen").
            #
            # So require the candidate to be doing something. role_agents is
            # the population this heuristic was written for (role-runner jobs,
            # spawned by the JVM one short bash per command); an invoking agent
            # covers a role job that has aged out of the rolling 20-job window.
            # A dormant agent -- f167-student has been :restored since the
            # 14:40 sweep, with no job and no invoke -- cannot own a running
            # process, whatever directory that process is sitting in. Keeping
            # the token match over this smaller set preserves the reason the
            # path exists: telling two CONCURRENT role runners apart, which
            # sole-role-job below cannot do.
            live_cands = set(role_agents) | {
                name for name, st in info.items()
                if (st[0] if isinstance(st, tuple) else st) == "invoking"}
            cands = []
            for cand in live_cands:
                toks = [t for t in cand.lower().split("-") if len(t) >= 3]
                if toks and all(t in path for t in toks):
                    cands.append((len(cand), cand))
            if cands:
                ident, how = max(cands)[1], "cwd"
        if not ident and sole_role and c["elapsed"] < 120 and not sid:
            ident, how = sole_role, "sole-role-job"
        # (5) registered-at, for a seat that never advertised a session id.
        # A Claude seat carries `--resume <sid>` only once it has been RESUMED;
        # a FRESH one is `claude --print --input-format stream-json ...` with no
        # uuid in argv (nor in environ, checked 2026-09-04), so (1)-(4) all miss
        # it and it stays in `unmatched` for its entire life. Two costs, both
        # observed on claude-4, 2026-09-07:
        #   - the panel printed a permanent "claude-4 (seat) ... up 32m12s"
        #     line, because the unmatched renderer has no equivalent of the
        #     busy() gate that hides an idle MATCHED seat -- so the rule "a
        #     sleeping seat gets no line" was being applied to some seats only,
        #     and the age shown was seat UPTIME read as a running turn, the
        #     exact confusion index.html:646-655 was written to end;
        #   - the agent never entered `used`, leaving it free for the +-20s
        #     start-time window to hand it a stranger's process (`lake env
        #     lean`, which belonged to f188-scribe).
        # likely_owner is the right key and was already being computed for the
        # unmatched row: a seat starts within seconds of a `registered-at` that
        # is stamped once and never expires. Restricted to seat-shaped direct
        # JVM children so a one-shot job cannot claim an agent this way, and it
        # yields to every deterministic path above. `cand not in used` keeps two
        # seats registered in one window from both answering to one name: the
        # second stays unmatched, which is honest, rather than mislabelled.
        if not ident and _seat_shaped(c):
            cand = likely_owner(c["start"])
            if cand and cand not in used:
                ident, how = cand, "registered-at"
        tree = subtree(child, 0, [14])
        if ident:
            if how != "sole-role-job":
                used.add(ident)
            status, act = info.get(ident, (None, None))
            # A seat's age is UPTIME, not an invocation duration: one
            # persistent `claude --print` per registered seat is the design,
            # it sleeps between turns (claude-10's seat started the second
            # its 5s job began, then state S for hours), and the roster
            # `status` riding this row is the same intent-not-liveness flag
            # the chips no longer trust (2026-09-04). The row reports the
            # seat; whether anything is RUNNING is what its child processes
            # say (the tree), and any activity shown is the seat's last
            # reported one, not proof of a running turn.
            # Children running while the agent has NO live job are work that
            # outlives the turn that started it: a detached script left behind
            # deliberately. Joe, 2026-09-07, on claude-1's f10-unblock-watch:
            # "I don't think there's any real concern. It's just not obvious
            # that this job is set up for a background run that takes place
            # between claude-1 turns." The tree showed the processes; nothing
            # said they were deferred rather than live, so a normal deferred
            # job read as an agent doing something unaccountable.
            # Mark work CARRIED OVER from an earlier turn, per process rather
            # than per agent. Keying it on the agent alone hid the very thing
            # Joe asked to see: claude-1's f10-unblock-watch is deferred work
            # whether or not claude-1 happens to be mid-turn right now, and the
            # moment it woke for an unrelated turn the label vanished. A
            # descendant that predates this turn's start was left behind by an
            # earlier one; if no turn is in flight, everything still running was.
            cutoff = turn_start.get(ident) if in_turn(ident) else None
            carried = mark_carried(tree, cutoff)
            rows.append({"id": ident, "status": "seat", "activity": act,
                         "matched-by": how, "pid": child,
                         "background": carried,
                         "elapsed": c["elapsed"], "tree": tree})
        else:
            tree["session"] = sid
            # Session id first: it is exact and durable, while likely_owner's
            # +-20s window around registered-at only fits a seat that started at
            # registration -- never one that respawned days later, which is the
            # ordinary case for a long-lived roster.
            tree["likely-agent"] = ((owner_of_session(sid) if sid else None)
                                    or likely_owner(c["start"]))
            unmatched.append(tree)
    return {"ok": True, "jvm": jvm, "agents": rows, "unmatched": unmatched}


# --- backlog: what is QUEUED and what NEEDS JOE, not just what is running.
# The chips/procs panels show work in progress; the boards (worklist.edn per
# lab) hold the queue and the operator-facing questions, and until now those
# were visible only through an agent narrating them (Joe, 2026-09-05: "what if
# voxterm was a bit more explicit about the backlog... which items are queued
# for processing, and which need clarification").
# worklist.edn is EDN, so parsing is delegated to bb (present on this box; the
# boards' own validators are bb scripts) and cached by mtime -- the wm-contract
# board is ~900KB and changes a few times an hour at most.

BACKLOG_BOARDS = [
    ("wm-contract",
     os.path.expanduser("~/code/futon2/holes/labs/wm-contract/worklist.edn")),
    ("zaif-harness",
     os.path.expanduser("~/code/futon2/holes/labs/zaif-harness/worklist.edn")),
]
PROPOSALS_DIR = os.path.expanduser(
    "~/code/futon2/holes/labs/wm-contract/proposals")
# The morning bulletin (:B1): the wm-build-loop writes one BULLETIN-<date>.md
# per session at its natural stop. The backlog panel carries a pointer to the
# latest one so the digest is reachable from the same place as the queue,
# rather than only through an agent narrating it.
BULLETIN_DIR = os.path.expanduser(
    "~/code/futon2/holes/labs/wm-contract/bulletins")
_BACKLOG_CACHE = {}  # path -> (mtime, rows)

_BB_BOARD_JSON = (
    "(require '[clojure.edn :as edn] '[cheshire.core :as json])"
    "(let [w (edn/read-string (slurp (first *command-line-args*)))"
    "      gist (fn [s] (let [s (str s)]"
    "                     (if (> (count s) 140) (str (subs s 0 140) \"...\") s)))"
    "      row (fn [i] {:id (name (:id i))"
    "                   :class (name (or (:class i) :?))"
    "                   :status (name (or (:status i) :?))"
    "                   :owner (str (or (:owner i) \"\"))"
    "                   :deps (mapv name (or (:depends-on i) []))"
    "                   :blocker (gist (or (:blocker i) \"\"))"
    "                   :ruling-blocked (boolean (and (= :blocked (:status i))"
    "                     (re-find #\"(?i)joe|ruling|reviewer decision\" (str (:blocker i)))))"
    "                   :gist (gist (:statement i))})]"
    "  (println (json/generate-string (mapv row (:items w)))))")


def _board_rows(path):
    """All rows of one board, parsed by bb, cached by mtime."""
    mtime = os.path.getmtime(path)
    hit = _BACKLOG_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    out = subprocess.run(["bb", "-e", _BB_BOARD_JSON, path],
                         capture_output=True, text=True, timeout=20)
    rows = json.loads(out.stdout) if out.returncode == 0 else []
    _BACKLOG_CACHE[path] = (mtime, rows)
    return rows


def latest_bulletin():
    """Newest BULLETIN-<date>.md, its date, size and age in seconds.

    Names sort as dates do (ISO), so the last name is the latest bulletin.
    The generator rewrites the file only when the day's content changes, so
    the mtime this reports is the age of the CONTENT, not of the last run.
    """
    try:
        names = sorted(n for n in os.listdir(BULLETIN_DIR)
                       if n.startswith("BULLETIN-") and n.endswith(".md"))
    except OSError:
        return None
    if not names:
        return None
    path = os.path.join(BULLETIN_DIR, names[-1])
    return {"name": names[-1],
            "path": path,
            "date": names[-1][len("BULLETIN-"):-len(".md")],
            "bytes": os.path.getsize(path),
            "age": int(time.time() - os.path.getmtime(path))}


def agency_backlog():
    boards, needs_joe = [], []
    for name, path in BACKLOG_BOARDS:
        if not os.path.exists(path):
            continue
        try:
            rows = _board_rows(path)
        except Exception:
            continue
        # "Needs clarification" is a fact the board records four ways: a row
        # owned by Joe, a row parked :needs-joe, a J-class (judgement) row
        # not yet done, or a blocked row whose :blocker text says its exit is
        # a ruling (2026-09-05: four ruling-blocked rows surfaced as one
        # because only the first three were read). Rendered apart because
        # these are the items only the operator can move.
        joes = [r for r in rows
                if ("joe" in r.get("owner", "").lower()
                    or r.get("status") == "needs-joe"
                    or (r.get("class") == "J" and r.get("status") != "done")
                    or r.get("ruling-blocked"))
                and r.get("status") != "done"]
        opens = [r for r in rows if r.get("status") == "open"]
        blocked = [r for r in rows
                   if r.get("status") == "blocked" and r not in joes]
        for r in joes:
            needs_joe.append(r | {"board": name})
        boards.append({"name": name,
                       "open": opens, "blocked": blocked,
                       "done": sum(1 for r in rows
                                   if r.get("status") == "done")})
    proposals = []
    if os.path.isdir(PROPOSALS_DIR):
        for f in sorted(os.listdir(PROPOSALS_DIR)):
            if f.endswith(".md"):
                proposals.append(
                    {"name": f,
                     "age": int(time.time()
                                - os.path.getmtime(
                                    os.path.join(PROPOSALS_DIR, f)))})
    return {"ok": True, "boards": boards, "needs_joe": needs_joe,
            "proposals": proposals, "bulletin": latest_bulletin()}


# ---------------------------------------------------------------------------
# APM loop visibility (Joe, 2026-09-06: "I've never had the assurance that I
# get from the WM build loop that this APM system is actually running ... it
# might be nice to have an APM build loop that was surfacing with some kind of
# red terminal error message, so I would visually know we're at a stuck
# terminal position").
#
# FILESYSTEM TRUTH, not the Agency. The frame ledgers, live/ receipts and the
# watchdog file are written by the coordinator as it works, so they answer
# "is anything happening?" even when the JVM or the jobs feed is down — which
# is exactly when the question matters most. The jobs feed is consulted only
# to decorate the strip with which fNN-role job is executing right now; its
# failure downgrades the strip, never blanks it.
#
# Why the chips alone were not enough: an fNN-role chip exists only while a
# role job is executing. Between phases (coordinator ticks, the ~4-minute
# memory-cascade expansion, promotion review) there is no job, so a healthy
# loop reads as silence — and a wedged loop reads exactly the same.
APM_ROOT = os.path.expanduser(
    os.environ.get("VOXTERM_APM_DIR", "~/code/futon3c/data/apm-campaigns"))
# No semantic progress AND no declared external wait for this long => stalled.
# Phase medians run 10-40 min (TN-apm-unattended-progress-contract), so 25 min
# of *unexplained* silence is meaningful; explained waits don't trip this.
APM_STALL_S = int(os.environ.get("VOXTERM_APM_STALL_S", "1500"))
# The watchdog writes an observation every couple of minutes; ten minutes of
# watchdog silence means the loop's own supervisor is gone, which is red
# regardless of what the ledger says.
APM_WATCHDOG_SILENT_S = int(os.environ.get("VOXTERM_APM_WATCHDOG_SILENT_S", "600"))


def _apm_read(path, head=0, tail=0):
    """Bounded read: whole file, first `head` bytes, or last `tail` bytes."""
    try:
        with open(path, "r", errors="replace") as f:
            if tail:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - tail))
                return f.read()
            if head:
                return f.read(head)
            return f.read()
    except OSError:
        return ""


def _apm_epoch(iso):
    """EDN :event/at (nanosecond ISO, Z suffix) -> epoch seconds, or None."""
    if not iso:
        return None
    try:
        from datetime import datetime
        trimmed = re.sub(r"(\.\d{1,6})\d*", r"\1", iso).replace("Z", "+00:00")
        return datetime.fromisoformat(trimmed).timestamp()
    except ValueError:
        return None


def _apm_newest_mtime(dirpath):
    newest = None
    try:
        for name in os.listdir(dirpath):
            p = os.path.join(dirpath, name)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    except OSError:
        pass
    return newest


def _apm_active_campaign():
    """The campaign dir most recently touched by its coordinator."""
    best, best_m = None, None
    try:
        entries = os.listdir(APM_ROOT)
    except OSError:
        return None
    # NOT the watchdog file: the watchdog sweeps every campaign's watchdog
    # file on each pass (all three carried 15:25 stamps on 2026-09-06), so
    # its mtime says "watchdog alive", not "this campaign is the live one".
    for name in entries:
        cdir = os.path.join(APM_ROOT, name)
        for probe in ("coordinator.edn", "queue-state.edn"):
            try:
                m = os.path.getmtime(os.path.join(cdir, probe))
            except OSError:
                continue
            if best_m is None or m > best_m:
                best, best_m = name, m
    return best


def _apm_frame_dirs(cdir, campaign):
    frames = []
    try:
        for name in os.listdir(cdir):
            m = re.fullmatch(re.escape(campaign) + r"-f(\d+)", name)
            if m and os.path.isdir(os.path.join(cdir, name)):
                frames.append((int(m.group(1)), os.path.join(cdir, name)))
    except OSError:
        pass
    return sorted(frames)


def _apm_frame_brief(num, fdir):
    """History row from bounded reads: the problem and how the frame ended."""
    # Single events run to tens of KB (an :event/body carries whole
    # certificates), so a small tail can miss every :event/type.
    head = _apm_read(os.path.join(fdir, "ledger.edn"), head=3000)
    tail = _apm_read(os.path.join(fdir, "ledger.edn"), tail=60000)
    prob = re.search(r':problem-id "([^"]+)"', head)
    types = re.findall(r":event/type :([a-z/-]+)", tail)
    last = types[-1] if types else None
    end = last or "?"
    if last == "frame/stopped":
        reason = re.search(r":reason :([a-z-]+)", tail)
        end = "VOID " + (reason.group(1) if reason else "?")
    elif last == "frame/closed":
        end = "closed+banked" if _apm_banked(fdir) else "closed"
    elif last == "frame/opened":
        end = "shell (never ran)"
    elif last == "frame/advanced":
        trans = re.findall(r":from :[a-z0-9-]+, :to :([a-z0-9-]+)", tail)
        end = "parked@" + (trans[-1] if trans else "?")
    return {"frame": "f%d" % num,
            "problem": prob.group(1) if prob else "?",
            "end": end}


def _apm_banked(fdir):
    """Closed-and-banked, not just possessing a terminal/ dir: voided frames
    write a frame-void certificate there too (f82, f105, ... 2026-09-06)."""
    head = _apm_read(os.path.join(fdir, "terminal", "frame-terminal.edn"),
                     head=2000)
    return ":frame/result :closed" in head


def _apm_running_jobs():
    """fNN-role jobs currently executing, per the Agency feed. Best effort.

    The feed's ?state= filter returns stale rows (finished solver jobs came
    back under state=running, 2026-09-06; same unreliability noted for the
    state filter on 08-30), so fetch unfiltered and judge each row's own
    state field."""
    jobs = []
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs?limit=120",
                     timeout=2.5) as r:
            feed = json.load(r)
        for j in feed.get("jobs", []):
            agent = str(j.get("agent-id") or "")
            if (j.get("state") == "running" and not j.get("finished-at")
                    and re.fullmatch(r"f\d+-[a-z-]+", agent)):
                started = j.get("started-at") or j.get("created-at")
                at = _apm_epoch(started)
                jobs.append({"agent": agent,
                             "for_s": int(time.time() - at) if at else None})
    except Exception:  # noqa: BLE001 — decoration only, never the verdict
        return None
    return jobs


# Topology build-loop visibility (Joe, 2026-09-06: "I can see the topology
# build loop is running. But I don't see what it's actually doing. It's just
# Sleep 20 topology contract, which doesn't really inspire any confidence at
# all").
#
# Same principle as the APM strip above: FILESYSTEM TRUTH. The supervisor
# writes runs/inflight.edn when it dispatches a row and runs/heartbeat.edn
# while it waits, so those two answer "what is it doing, and is it still
# doing it?" without asking the Agency. The loop blocks synchronously on a
# job for up to JOB_TIMEOUT (4h), so long silences are normal and only
# heartbeat staleness is evidence of trouble.
TOPO_LAB = os.path.expanduser(os.environ.get(
    "VOXTERM_TOPO_DIR", "~/code/apm-lean/holes/labs/topology-contract"))
# The supervisor polls every POLL_SECONDS (20) and rewrites the heartbeat each
# time, so two minutes of heartbeat silence means the supervisor itself is gone
# -- distinct from a seat simply taking a long time on one row.
TOPO_SILENT_S = int(os.environ.get("VOXTERM_TOPO_SILENT_S", "120"))


def _topo_edn_field(text, key, quoted=True):
    m = re.search(r':%s\s+"([^"]*)"' % key, text) if quoted else \
        re.search(r':%s\s+([^\s,}]+)' % key, text)
    return m.group(1) if m else None


def _topo_supervisor_state(runs):
    """Is the supervisor wrapping this loop still up? Ask its own log.

    The loop writes STOP: into build-loop.log through log(), but its two
    CLEAN exits go out through notify() only -- which sends a notice and
    writes nothing to that file:

        notify "PAUSED: no runnable rows; owner decision or strategy rewrite required"
        notify "DONE: no open or unreviewed rows"

    So the PAUSED|DONE alternation in the build-loop.log scan below has never
    matched anything, and a track that finished left no trace in the file this
    strip was reading. Track 1 exited cleanly at 18:59:54 on 2026-09-09 with
    one :needs-owner row and reported as "supervisor silent 936s" -- red,
    wrong, and pointing at the apparatus instead of at the decision it was
    waiting for.

    topology-supervisor.sh logs every start and every exit, so it is the
    record of whether the supervisor is running. Returns None while it is up,
    including between a repairable loop failure and the restart that follows.
    """
    text = _apm_read(os.path.join(runs, "supervisor.log"), tail=8000)
    marks = list(re.finditer(
        r"supervisor: (starting loop|loop exited[^\n]*"
        r"|not auto-repairable[^\n]*)", text))
    if not marks:
        return None
    last = marks[-1].group(1).strip()
    # "restarting loop" is reached via "supervisor: healthy after Ns;
    # restarting loop", which does not match -- the next line's "supervisor:
    # starting loop" is what marks the loop back up.
    if last.startswith("starting loop"):
        return None
    return {"state": "finished" if "rc=0" in last else "stopped",
            "reason": last}


def _topology_track(track, runs_name, worklist_name):
    """One build loop, read from its own run directory.

    Each campaign gets its own TOPOLOGY_RUNS and TOPOLOGY_LEDGER (apm-lean
    45f338c3), so a second track is a second (runs2, worklist2.edn) pair in
    the same lab rather than a second lab.
    """
    now = time.time()
    runs = os.path.join(TOPO_LAB, runs_name)
    inflight = _apm_read(os.path.join(runs, "inflight.edn"))
    heartbeat = _apm_read(os.path.join(runs, "heartbeat.edn"))
    log = _apm_read(os.path.join(runs, "build-loop.log"), tail=20000)
    worklist = _apm_read(os.path.join(TOPO_LAB, worklist_name))

    if not os.path.isdir(runs):
        return {"ok": False, "track": track,
                "error": "no run directory at " + runs}

    row = _topo_edn_field(inflight, "row")
    seat = _topo_edn_field(inflight, "seat")
    dispatched = _topo_edn_field(inflight, "dispatched-at")
    hb_at = _topo_edn_field(heartbeat, "checked-at", quoted=False)

    # Phase comes from the log's last work(...)/review(...) mention, which is
    # the only place the supervisor names which half of the cycle it is in.
    phases = re.findall(r"\] (work|review)\(([a-z0-9-]+)\)", log)
    phase, log_row = (phases[-1] if phases else (None, None))
    row = row or log_row

    hb_age = None
    if hb_at:
        e = _apm_epoch(hb_at)
        if e:
            hb_age = int(now - e)
    disp_age = None
    if dispatched:
        e = _apm_epoch(dispatched)
        if e:
            disp_age = int(now - e)

    # Worklist states: take the checker's OWN authoritative line rather than
    # re-parsing the ledger here. A naive ":state :x" regex over worklist.edn
    # counts nested evidence maps too (39 states for a 21-item ledger), and a
    # visibility strip that overstates progress is worse than none. The
    # supervisor runs worklist_check.bb every iteration and logs
    #   topology-worklist: 21 items OK; {:done 18, :open 1}
    states, total = {}, 0
    counts = re.findall(r"topology-worklist: (\d+) items OK; \{([^}]*)\}", log)
    if counts:
        total = int(counts[-1][0])
        for k, v in re.findall(r":([a-z-]+) (\d+)", counts[-1][1]):
            states[k] = int(v)

    # Rows finished since the log began, newest last: "applied pass to X".
    applied = re.findall(r"applied (pass|fail) to ([a-z0-9-]+)", log)
    recent = [{"row": r, "outcome": o} for o, r in applied[-6:]]

    # The supervisor announces every deliberate exit on its last log line, and
    # they are NOT all alarming: "STOP: MAX_ITER=60 exhausted" means it ran its
    # whole budget and finished, which is routine, while "STOP: ledger
    # validation failed" is a real halt. Reporting either as "supervisor
    # silent" -- which is what this did on 2026-09-07 after a clean
    # MAX_ITER exit -- trains the operator to discount the strip.
    tail = log[-4000:]
    # Position matters: an exit line only means the loop is down if nothing
    # happened AFTER it. A restarted supervisor leaves its predecessor's
    # "STOP:" inside the same tail, and reading that as current would report a
    # working loop as finished -- the mirror of the mistake being fixed here.
    exits = list(re.finditer(r"(PAUSED|DONE|STOP): ([^\n]+)", tail))
    activity = list(re.finditer(r"\] (work|review)\([a-z0-9-]+\) (dispatch|job|heartbeat|done)",
                                tail))
    last_exit = exits[-1] if exits else None
    last_activity = activity[-1] if activity else None
    exited = (last_exit
              if last_exit and (not last_activity
                                or last_exit.start() > last_activity.start())
              else None)
    routine_exit = bool(exited and re.search(r"MAX_ITER|no open or unreviewed",
                                             exited.group(2)))
    supervisor = _topo_supervisor_state(runs)
    if supervisor:
        state = supervisor["state"]
        alert = supervisor["reason"]
        # What a finished track is actually waiting for. The loop pauses on
        # exactly these two states, and the count is the checker's own.
        waiting = states.get("needs-owner", 0) + states.get("blocked", 0)
        if state == "finished":
            alert = "finished cleanly" + (
                "; %d row%s awaiting an owner decision"
                % (waiting, "" if waiting == 1 else "s") if waiting else "")
    elif exited:
        state = "finished" if routine_exit else "stopped"
        alert = exited.group(1) + ": " + exited.group(2).strip()
    elif hb_age is not None and hb_age > TOPO_SILENT_S:
        state, alert = "stalled", "supervisor silent %ds" % hb_age
    elif row:
        state, alert = "running", None
    else:
        state, alert = "idle", "no row dispatched"

    return {"ok": True, "lab": os.path.basename(TOPO_LAB), "track": track,
            "row": row, "seat": seat, "phase": phase,
            "state": state, "alert": alert,
            "for_s": disp_age, "heartbeat_s": hb_age,
            "states": states, "rows_total": total,
            "done": states.get("done", 0), "recent": recent}


def _topology_tracks():
    """(track, runs, worklist) for every campaign in the lab.

    Track 1 is the original pair. Any runs<N> with a matching worklist<N>.edn
    joins automatically, so standing up a third campaign needs no change here.
    """
    found = [("1", "runs", "worklist.edn")]
    try:
        for name in sorted(os.listdir(TOPO_LAB)):
            m = re.fullmatch(r"runs(\d+)", name)
            if not m:
                continue
            wl = "worklist%s.edn" % m.group(1)
            if os.path.isfile(os.path.join(TOPO_LAB, wl)):
                found.append((m.group(1), name, wl))
    except OSError:
        pass
    return found


def topology_status():
    """Every track, so a second campaign is watchable rather than invisible.

    Track 1's fields stay at the top level: this endpoint had a flat shape
    before there was more than one loop, and a client that has not been
    updated should keep showing the first campaign rather than nothing.
    """
    tracks = [_topology_track(*t) for t in _topology_tracks()]
    first = tracks[0] if tracks else {"ok": False, "error": "no topology lab"}
    return dict(first, tracks=tracks)


def _jvm_health():
    """The JVM's own memory, from the Agency's O(1) /health block.

    A long APM phase runs IN-PROCESS: the regulator claims a tick, dispatches
    no job, and the watchdog goes quiet because there is nothing external to
    observe. That is indistinguishable from a wedge unless you can see inside.
    On 2026-09-07 this JVM ate 4095 MB of a 4096 MB direct-buffer limit over
    8.8 days and the first symptom was the campaign halting; the exhaustion
    itself was never on screen. It is now.
    """
    try:
        with urlopen("http://127.0.0.1:7070/health", timeout=3) as r:
            return (json.loads(r.read().decode("utf-8")) or {}).get("jvm")
    except Exception:
        return None


def _apm_frame_timeline(fdir, campaign, frame):
    """The frame as a sequence of phases with durations, each linked to the
    agent turn that ran it -- or named as in-process work when no agent did.

    Joe, 2026-09-07: "how student attempt one relates to the F188 student ...
    is not obvious looking at the system ... I just don't have any real
    visibility into the things that aren't specifically agent interactions
    inside of the loop."

    The strip showed a phase name, and the agent list separately showed a job,
    with nothing connecting them and nothing at all for the stretches between
    agent turns -- which is where a frame spends much of its time (f188's
    memory cascade alone ran 8m11s with no agent involved).
    """
    ledger = _apm_read(os.path.join(fdir, "ledger.edn"), tail=200000)
    if not ledger:
        return None
    # (:from :x, :to :y) paired with the :event/at that precedes it
    events = []
    for m in re.finditer(r':event/at "([^"]+)".{0,400}?:from :([a-z0-9-]+), :to :([a-z0-9-]+)',
                         ledger, re.S):
        at, frm, to = m.group(1), m.group(2), m.group(3)
        if not events or events[-1][1:] != (frm, to):
            events.append((at, frm, to))
    if not events:
        return None
    jobs = _apm_running_jobs()
    now = time.time()
    phases = []
    for i, (at, frm, to) in enumerate(events):
        start = _apm_epoch(at)
        end = _apm_epoch(events[i + 1][0]) if i + 1 < len(events) else None
        phases.append({"phase": frm if i == 0 else None})
        phases[-1] = {"phase": to,
                      "started_at": at,
                      "duration_s": int((end or now) - start) if start else None,
                      "current": end is None}
    # Link the live phase to an agent turn for THIS frame, if one is running.
    cur = phases[-1] if phases else None
    if cur:
        role = None
        for j in (jobs or []):
            a = str(j.get("agent") or "")
            if a.startswith(frame + "-"):
                role = {"agent": a, "for_s": j.get("for_s")}
                break
        cur["agent"] = role
        # No agent turn means the loop itself is working -- name it, so a quiet
        # stretch reads as in-process work rather than as nothing happening.
        #
        # But a feed we could not READ is neither. _apm_running_jobs returns
        # None on any Agency failure and iterating that raised "'NoneType'
        # object is not iterable" out of apm_status, so one 2.5s timeout
        # replaced the whole strip with a Python error (Joe saw it 2026-09-09,
        # during the topology loop's repair burst). Its own comment says the
        # feed is "decoration only, never the verdict"; it had become the
        # verdict. Guarding it alone would trade the crash for a quieter lie --
        # "in-process" asserts the loop is working, which an unread feed does
        # not establish -- so an unavailable feed says so.
        cur["actor"] = ("agent" if role
                        else "in-process" if jobs is not None
                        else "unknown")
    return phases[-8:]


def _apm_phase_detail(fdir, phase):
    """What the CURRENT phase is doing, from its own live/<phase>.edn.

    Joe, 2026-09-07: "right now it just says... Progress nine minutes ago, and
    that makes me nervous when, in fact, it's actually working, doing something
    useful." Elapsed-since-last-progress is a lagging measure: a phase can be
    working hard for ten minutes and move nothing a ledger would notice. The
    phase files carry the live detail -- stage, retry budget, and the finding
    that caused the last failure -- so read that instead of inferring from
    silence.

    Read the LAST error and pair its message by proximity, never the first
    match of each field independently. A phase file is an append-only history,
    so a plain re.search returns the OLDEST error in it and keeps returning it
    forever. On 2026-09-08 f193 rendered :error/code from byte 3529 beside
    :error/message from byte 31648 -- 28KB and many events apart -- producing
    "report-edn-lint-failed / Atomic memory assertion transport failed", a pair
    that never occurred. Joe read that chimera as a recurring error class over
    several hours and was chasing a rendering artifact. The real terminal error
    sat at byte 31191, :live-job-transport-retry-exhausted, and was never shown.

    Also carry the file's age. This detail described a frame that had been dead
    for an hour while the strip reported state ok, so a stale detail must say
    so rather than present as current.
    """
    if not phase:
        return None
    path = os.path.join(fdir, "live", phase + ".edn")
    raw = _apm_read(path, tail=20000)
    if not raw:
        return None
    try:
        age_s = int(time.time() - os.path.getmtime(path))
    except OSError:
        age_s = None

    def last(pat):
        ms = list(re.finditer(pat, raw))
        return ms[-1] if ms else None

    def field(k, quoted=False):
        m = last(r':%s\s+"([^"]*)"' % k if quoted else r':%s\s+([^\s,}\]]+)' % k)
        return m.group(1) if m else None

    stage = field("stage")
    att = field(r"transport-retry/attempt")
    mx = field(r"transport-retry/max-attempts")
    nb = field(r"transport-retry/not-before-ms")
    rk = field(r"repair/kind")
    ra = field(r"repair/attempts")
    rm = field(r"repair/max-attempts")

    # Anchor on the last error, then take the message nearest it. Unpaired is
    # reported as None: a message from an unrelated event is worse than none.
    err = None
    msg = None
    em = last(r':error/code\s+([^\s,}\]]+)')
    if em:
        err = em.group(1)
        best = None
        for mm in re.finditer(r':error/message\s+"([^"]{0,160})"', raw):
            d = abs(mm.start() - em.start())
            if d <= 2000 and (best is None or d < best[0]):
                best = (d, mm.group(1))
        msg = best[1] if best else None

    retry_in = None
    if nb and nb.isdigit():
        retry_in = int((int(nb) - time.time() * 1000) / 1000)
    return {"stage": stage, "error_code": err, "error_message": msg,
            "detail_age_s": age_s,
            "retry_attempt": att, "retry_max": mx, "retry_in_s": retry_in,
            "repair_kind": rk, "repair_attempts": ra, "repair_max": rm}


def _substrate_permits():
    """futon1b's concurrency gate. Two permits total; when both are held every
    authoritative read queues, and the promotion's per-read bound is 5s. That
    is what timed out f188's post-publication verification, with the direct
    buffers healthy -- so a green JVM row alone would have been misleading."""
    try:
        with urlopen("http://127.0.0.1:7073/health", timeout=3) as r:
            t = r.read().decode("utf-8", "replace")
        tot = re.search(r':permits/total (\d+)', t)
        avail = re.search(r':permits/available (\d+)', t)
        holders = len(re.findall(r':age-ms (\d+)', t))
        return {"total": int(tot.group(1)) if tot else None,
                "available": int(avail.group(1)) if avail else None,
                "holders": holders,
                "node_open": ":node-open? true" in t}
    except Exception:
        return None


_APM_PARK_CACHE = {}
_APM_PARK_LOCK = threading.Lock()


def _apm_parked_decisions(cdir):
    """Project top-level park records with an EDN reader, never text proximity.

    The queue contains nested historical reports with the same field names.
    Cache by file identity/mtime/size to avoid a Babashka process each UI poll.
    Reader failures remain visible instead of looking like an empty queue.
    """
    path = os.path.join(cdir, "queue-state.edn")
    try:
        with _APM_PARK_LOCK:
            stat = os.stat(path)
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = _APM_PARK_CACHE.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
            result = subprocess.run(
                ["bb", os.path.join(HERE, "apm_parked_projection.clj"), path],
                capture_output=True, text=True, timeout=5, check=True)
            projection = {"rows": json.loads(result.stdout), "error": None}
            _APM_PARK_CACHE[path] = (stamp, projection)
            return projection
    except FileNotFoundError as exc:
        # A campaign may legitimately have no queue yet; a missing bb is an error.
        if not os.path.exists(path):
            return {"rows": [], "error": None}
        return {"rows": [], "error": "parked queue unreadable: " + str(exc)}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"rows": [], "error": "parked queue unreadable: " + str(exc)}


def _apm_park_label(row):
    """Name a park as problem@phase.

    Without the phase a parked learning measurement is indistinguishable from
    a parked problem. f206/m02A06 parked at student-attempt-2 while its proof
    was already certified, verified and landed on apm-lean master, and the
    strip said only "m02A06" -- which reads as an unsolved problem.
    """
    name = row.get("problem") or row.get("frame")
    if not name:
        return None
    phase = row.get("phase")
    return name + "@" + phase if phase else name


def apm_status():
    now = time.time()
    campaign = _apm_active_campaign()
    if not campaign:
        return {"ok": False, "error": "no campaign dirs under " + APM_ROOT}
    cdir = os.path.join(APM_ROOT, campaign)
    frames = _apm_frame_dirs(cdir, campaign)
    if not frames:
        return {"ok": True, "campaign": campaign, "state": "idle",
                "alert": None, "detail": "campaign has no frames yet",
                "recent": [], "jobs": _apm_running_jobs()}
    num, fdir = frames[-1]
    ledger = _apm_read(os.path.join(fdir, "ledger.edn"))

    prob = re.search(r':problem-id "([^"]+)"', ledger)
    problem = prob.group(1) if prob else "?"
    types = re.findall(r":event/type :([a-z/-]+)", ledger)
    last_type = types[-1] if types else None
    ats = re.findall(r':event/at "([^"]+)"', ledger)
    last_event = _apm_epoch(ats[-1]) if ats else None
    trans = re.findall(r":from :[a-z0-9-]+, :to :([a-z0-9-]+)", ledger)
    phase = trans[-1] if trans else (
        "preflight" if last_type in ("frame/opened", "frame/advanced") else "?")

    # Freshest write anywhere in the frame: live/ receipts land mid-phase,
    # so this moves when the ledger doesn't.
    live_m = _apm_newest_mtime(os.path.join(fdir, "live"))
    activity = max(x for x in [last_event, live_m, 0] if x is not None)

    wd = _apm_read(os.path.join(cdir, "coordinator.edn.watchdog.edn"))
    wd_status_m = re.search(r":watchdog/status :([a-z-]+)", wd)
    wd_status = wd_status_m.group(1) if wd_status_m else None
    wd_progress = re.search(r":watchdog/last-progress-ms (\d+)", wd)
    wd_observed = re.search(r":watchdog/observed-at-ms (\d+)", wd)
    # This flag lives INSIDE :watchdog/trace-observation, not as a
    # :watchdog/ key -- it is the watchdog's own verdict that the current wait
    # is legitimate, and it is what suppresses the stall alarm. Scope the match
    # to that map rather than the whole file, so an identically-named field
    # appearing anywhere else cannot silence a real stall.
    _trace = re.search(r":watchdog/trace-observation \{([^}]*)\}", wd)
    wd_valid_wait = bool(_trace and ":valid-external-wait? true" in _trace.group(1))
    wd_violation = ":first-violation-recorded? true" in wd
    wd_disabled = ":coordinator-disabled? true" in wd
    observed_s = (now - int(wd_observed.group(1)) / 1000.0) if wd_observed else None
    progress_s = (now - int(wd_progress.group(1)) / 1000.0) if wd_progress else \
        (now - activity if activity else None)

    # The substrate-wait state (commit d6beeec0): the queue holds instead of
    # churning when a provider limit is hit. Must match an actual :status
    # assignment — the bare keyword also appears in every tick's
    # :status/one-of postcondition vocabulary.
    waiting_substrate = ":status :awaiting-substrate" in _apm_read(
        os.path.join(cdir, "coordinator.edn"), tail=6000)

    # The systematic brake (three consecutive identical role-terminal parks
    # or voids) stops the whole queue; that is a red state in its own right,
    # not a slow-burning stall.
    systematic = ":failed-systematic-frame-failure" in _apm_read(
        os.path.join(cdir, "queue-state.edn"))

    # A watchdog HALT disables the durable coordinator, and the watchdog state
    # file is then overwritten wholesale on the next watching cycle -- so once
    # it recovers, every trace of the halt is gone except the rearm journal.
    # Joe saw "coordinator disabled" and by the time it was investigated there
    # was nothing left to look at; the halt could not even be dated. A
    # self-healing fault that leaves no record is one nobody can act on, so
    # recent rearms ride alongside the state rather than inside it.
    rearms = []
    try:
        rearm_text = _apm_read(
            os.path.join(cdir, "coordinator.edn.watchdog-rearms.edn"), tail=20000)
        for m in re.finditer(r":watchdog/rearm-attempts-ms \[([^\]]*)\]", rearm_text):
            for ms in re.findall(r"\d+", m.group(1)):
                age = now - int(ms) / 1000.0
                if 0 <= age <= 21600:            # six hours
                    rearms.append({"at_ms": int(ms), "age_s": int(age)})
    except Exception:
        pass
    rearms.sort(key=lambda r: r["at_ms"])

    state, alert = "ok", None
    if systematic:
        state = "stopped"
        alert = "QUEUE STOPPED: systematic frame failure (3x identical)"
    elif last_type == "frame/stopped":
        reason = re.search(r":reason :([a-z-]+)", ledger[-6000:])
        inv = re.search(r":failed-invariants \[([^\]]*)\]", ledger[-6000:])
        state = "stopped"
        alert = ("f%d STOPPED %s" % (num, reason.group(1) if reason else "?")
                 + (" [" + inv.group(1).replace(":", "") + "]" if inv else ""))
    elif waiting_substrate and (progress_s is None or progress_s > 300):
        state = "waiting"
        alert = "queue holding: substrate unavailable (provider limit?)"
    elif wd and observed_s is not None and observed_s > APM_WATCHDOG_SILENT_S:
        state = "stalled"
        alert = "watchdog silent %dm — loop supervisor gone" % (observed_s // 60)
    elif wd_disabled:
        state = "stalled"
        alert = "coordinator disabled"
    elif wd_violation:
        state = "stalled"
        alert = "watchdog recorded a progress violation"
    elif (progress_s is not None and progress_s > APM_STALL_S
          and not wd_valid_wait):
        state = "stalled"
        alert = "no progress %dm and no declared wait" % (progress_s // 60)
    elif last_type == "frame/closed":
        state = "closed"

    # Cascade expansion runs inside a tick with no job to watch; its own
    # record says whether the quiet minutes are it.
    cascade = None
    cascade_seeds = None
    cascade_where = ""
    cop = _apm_read(os.path.join(fdir, "live", "memory-cascade-operation.edn"))
    if cop:
        cst = re.search(r":status :([a-z-]+)", cop)
        cascade = cst.group(1) if cst else None
        csd = re.search(r":seed-count (\d+)", cop)
        if csd:
            cascade_seeds = int(csd.group(1))
        cph = re.search(r":phase :([a-z0-9-]+)", cop)
        cat = re.search(r':finished-at "\d{4}-\d\d-\d\dT(\d\d:\d\d)', cop)
        # The record is a past event on this frame; without where and when,
        # a reload that still shows it reads as "not fixed" (f215).
        cascade_where = " ".join(filter(None, [
            cph and "at " + cph.group(1), cat and cat.group(1) + "Z"]))
    # A dead cascade doesn't stall the frame, but it changes what the frame is
    # measuring, so it stays an alarm even while phases advance (f187).
    #
    # Two different faults were reported with one sentence. The shelf can be
    # empty -- f51/A10, the frame measures a dead transport and the datum is
    # void -- or seeds can be served and only the expansion over them fail.
    # f193 was the second: 313 ids served, 313 seeds recorded, expansion
    # dead. This line said "running WITHOUT served memory" for both, which
    # was simply false on f193 and sent the diagnosis to the wrong layer.
    if cascade == "failed" and state == "ok":
        state = "degraded"
        if cascade_seeds:
            alert = ("cascade expansion failed%s: %d seeds served, not expanded"
                     % (cascade_where and " " + cascade_where, cascade_seeds))
        elif cascade_seeds == 0:
            alert = "cascade failed: frame is running WITHOUT served memory"
        else:
            alert = "cascade failed: served memory unknown (no seed count)"

    recent = [_apm_frame_brief(n, d) for n, d in frames[-6:-1]][::-1]
    banked = [n for n, d in frames if _apm_banked(d)]
    # A parked frame does not stop the campaign -- the queue advances to the
    # next problem -- so `state` stays "ok" and the strip stayed silent while
    # decisions piled up. Surface them without pretending the campaign is down.
    park_projection = _apm_parked_decisions(cdir)
    parked = park_projection["rows"]
    if parked and not alert:
        alert = "%d parked, awaiting decision: %s" % (
            len(parked), ", ".join(filter(None, map(_apm_park_label,
                                                    parked[:3]))))

    return {"ok": True, "campaign": campaign, "frame": "f%d" % num,
            "problem": problem, "phase": phase, "state": state, "alert": alert,
            "last_progress_s": int(progress_s) if progress_s is not None else None,
            "last_write_s": int(now - activity) if activity else None,
            "watchdog": wd_status, "valid_wait": wd_valid_wait,
            "jvm": _jvm_health(),
            "phase_detail": _apm_phase_detail(fdir, phase),
            "timeline": _apm_frame_timeline(fdir, campaign, "f%d" % num),
            "substrate": _substrate_permits(),
            "cascade": cascade, "recent": recent,
            "banked": ["f%d" % n for n in banked[-3:]],
            "banked_count": len(banked),
            "parked_decisions": parked,
            "parked_decisions_error": park_projection["error"],
            "coordinator_rearms": rearms,
            "jobs": _apm_running_jobs()}


# ---------------------------------------------------------------------------
# Subscription usage (Joe, 2026-09-06: "it makes me a little nervous not
# knowing how much percentage of usage I have left ... the weekly usage as a
# percentage for each of the different subscriptions").
#
# WEEKLY IS THE NUMBER. Each provider also exposes a short rolling window
# (Claude's 5-hour session, Zai's 5-hour pool) and those are carried along, but
# the weekly figure is what the panel leads with, because it is the one you can
# schedule around.
#
# THREE DIFFERENT MECHANISMS, none of which is documented together anywhere:
#   claude — GET /api/oauth/usage on api.anthropic.com with the Claude Code
#            OAuth access token. Returns `seven_day.utilization` as a percent.
#   codex  — the Codex app-server's JSON-RPC method `account/rateLimits/read`
#            over stdio. NOT an HTTP call: Codex learns its limits from turn
#            responses, and this is the only read-only path that does not spend
#            a turn. `windowDurationMins == 10080` is the weekly window.
#   zai    — GET /api/monitor/usage/quota/limit on api.z.ai. Returns a `limits`
#            list; `unit` is the window kind (3 = hour, 5 = month, 6 = week) and
#            `percentage` is percent USED.
#
# EVERY FIELD BELOW WAS READ OFF A LIVE RESPONSE ON 2026-09-06, not inferred
# from docs. The shapes are stable enough to parse defensively but not stable
# enough to trust blindly, hence: one provider failing never blanks the others,
# and a provider that cannot be read reports an ERROR STRING rather than a
# number. A panel that shows "100%" because a token expired is worse than one
# that shows "unreadable" -- it is exactly the false reassurance this feature
# exists to remove.
USAGE_TTL_S = float(os.environ.get("VOXTERM_USAGE_TTL", "120"))
_usage_lock = threading.Lock()
_usage_cache = {"at": 0.0, "data": None}


def _usage_claude():
    p = os.path.expanduser("~/.claude/.credentials.json")
    with open(p) as f:
        oauth = json.load(f)["claudeAiOauth"]
    tok = oauth["accessToken"]
    exp = oauth.get("expiresAt")
    if exp and time.time() * 1000 > exp:
        # Say so rather than firing a request that will 401: an expired token is
        # a "run claude and it refreshes" problem, not an outage.
        return {"error": "oauth token expired — start claude once to refresh"}
    req = Request("https://api.anthropic.com/api/oauth/usage",
                  headers={"Authorization": "Bearer " + tok,
                           "anthropic-beta": "oauth-2025-04-20"})
    with urlopen(req, timeout=15) as r:
        d = json.load(r)
    out = {"plan": oauth.get("subscriptionType"), "tier": oauth.get("rateLimitTier")}
    wk, sess = d.get("seven_day") or {}, d.get("five_hour") or {}
    if wk.get("utilization") is None:
        return {"error": "no seven_day bucket in response"}
    out["weekly_used_pct"] = float(wk["utilization"])
    out["weekly_resets_at"] = wk.get("resets_at")
    if sess.get("utilization") is not None:
        out["session_used_pct"] = float(sess["utilization"])
        out["session_resets_at"] = sess.get("resets_at")
    return out


def _usage_codex():
    # stdio JSON-RPC. Spawning a process per poll is why USAGE_TTL_S exists.
    proc = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        for i, m in ((1, "initialize"), (2, "account/rateLimits/read")):
            params = {"clientInfo": {"name": "voxterm-usage", "version": "1"}} if i == 1 else {}
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m,
                                         "params": params}) + "\n")
        proc.stdin.flush()
        result, deadline = None, time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                result = msg.get("result")
                break
    finally:
        proc.kill()
    if not result:
        return {"error": "no response from codex app-server"}
    snap = result.get("rateLimits") or {}
    out = {"plan": snap.get("planType")}
    # The weekly window is identified by its DURATION, not its position: the
    # `codex` bucket puts it in `primary` while the per-model buckets put a
    # 5-hour window there and the week in `secondary`.
    for slot in ("primary", "secondary"):
        w = snap.get(slot) or {}
        if w.get("windowDurationMins") == 10080:
            out["weekly_used_pct"] = float(w.get("usedPercent", 0))
            out["weekly_resets_at"] = w.get("resetsAt")
        elif w.get("windowDurationMins"):
            out["session_used_pct"] = float(w.get("usedPercent", 0))
            out["session_resets_at"] = w.get("resetsAt")
    if "weekly_used_pct" not in out:
        return {"error": "no 10080-minute window in snapshot"}
    credits = (result.get("rateLimitResetCredits") or {}).get("availableCount")
    if credits:
        out["reset_credits"] = credits
    return out


def _usage_zai():
    key = None
    for p in ("~/.zaikey", "~/.zai-key"):
        try:
            with open(os.path.expanduser(p)) as f:
                key = f.read().strip()
            break
        except OSError:
            continue
    key = os.environ.get("ZAI_API_KEY") or key
    if not key:
        return {"error": "no ZAI_API_KEY / ~/.zai-key"}
    req = Request("https://api.z.ai/api/monitor/usage/quota/limit",
                  headers={"Authorization": "Bearer " + key})
    with urlopen(req, timeout=15) as r:
        d = json.load(r)
    if not d.get("success"):
        return {"error": "z.ai: " + str(d.get("msg"))}
    data = d.get("data") or {}
    out = {"plan": data.get("level")}
    for lim in data.get("limits") or []:
        unit, pct = lim.get("unit"), lim.get("percentage")
        if pct is None:
            continue
        if unit == 6:                      # week
            out["weekly_used_pct"] = float(pct)
            out["weekly_resets_at"] = lim.get("nextResetTime")
        elif unit == 3:                    # hour(s) — the 5-hour prompt pool
            out["session_used_pct"] = float(pct)
            out["session_resets_at"] = lim.get("nextResetTime")
    if "weekly_used_pct" not in out:
        return {"error": "no weekly (unit 6) limit in response"}
    return out


def _reset_epoch(v):
    """Normalise a reset time to epoch SECONDS.

    The three providers disagree: Claude returns an ISO-8601 string, Codex
    epoch seconds, Zai epoch milliseconds. Normalising here rather than in the
    browser means the panel has one format to render and the magnitude test
    lives next to the evidence for it.
    """
    if v is None:
        return None
    if isinstance(v, str):
        try:
            import datetime as _dt
            return _dt.datetime.fromisoformat(v).timestamp()
        except ValueError:
            return None
    v = float(v)
    # Milliseconds if it lands centuries in the future when read as seconds.
    return v / 1000.0 if v > 1e11 else v


def collect_usage():
    """Weekly usage for every subscription. One provider's failure is its own."""
    with _usage_lock:
        now = time.time()
        if _usage_cache["data"] and now - _usage_cache["at"] < USAGE_TTL_S:
            return _usage_cache["data"]
    providers = {}
    for name, fn in (("claude", _usage_claude), ("codex", _usage_codex), ("zai", _usage_zai)):
        t0 = time.time()
        try:
            providers[name] = fn()
        except Exception as e:                      # noqa: BLE001 - report, never raise
            providers[name] = {"error": "%s: %s" % (type(e).__name__, e)}
        providers[name]["took_ms"] = int((time.time() - t0) * 1000)
        # Derived once, here, so the browser never does percentage arithmetic:
        # "left" is what Joe asked to see and the only place it should be computed.
        if "weekly_used_pct" in providers[name]:
            providers[name]["weekly_left_pct"] = round(100.0 - providers[name]["weekly_used_pct"], 1)
        for k in ("weekly_resets_at", "session_resets_at"):
            if k in providers[name]:
                providers[name][k] = _reset_epoch(providers[name][k])
    data = {"ok": True, "fetched_at": time.time(), "providers": providers}
    with _usage_lock:
        _usage_cache.update(at=time.time(), data=data)
    return data


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
        elif path == "/usage":
            # Weekly subscription headroom. Cached (USAGE_TTL_S) because the
            # codex leg spawns a process; the numbers move slowly enough that a
            # two-minute-old reading is still an honest one.
            try:
                self._send(200, json.dumps(collect_usage()), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "error": str(e)}),
                           "application/json")
        elif path == "/agency/runtimes":
            runtimes = runtime_choices()
            self._send(200, json.dumps({"ok": True, "runtimes": runtimes}),
                       "application/json")
        elif path == "/agency/active":
            # Who is worth tapping: the agents working now, then the ones that ran
            # recently. Neither source answers that alone.
            #
            # Agent `status` is the only thing that sees an agent the operator is
            # talking to directly -- claude-7 had zero entries in the job feed while
            # its own status read "invoking" (2026-08-29), because /invoke/jobs only
            # records agent-to-agent dispatch.
            #
            # But `last-active` cannot supply "recently": a restore sweep rewrites it
            # across the whole roster at once (82 agents stamped 19:28:20 the same
            # day), so recency has to come from job start times, which are per
            # dispatch and never rewritten.
            try:
                rows, seen = [], set()

                # The model rides along because the chips are the only place the
                # choice is ever visible again: "+ new -> Fable" says so once in
                # the status line and then you are looking at "claude-11", which
                # says nothing about what it costs to talk to.
                #
                # Most seats never declared a model at registration (only
                # claude-1 did, 2026-09-06 survey), so declaration alone left
                # every other chip blank — Joe: "That's the level of
                # transparency that I want to have." Resolution order, best
                # evidence first, each rung something measured rather than
                # assumed:
                #   1. roster metadata.model (declared at registration);
                #   2. claude seats: the session .jsonl records the model of
                #      every turn — exact for THIS seat, read from its tail;
                #   3. codex seats: latest turn_context in THIS session's
                #      rollout log. A different seat's newest model is not
                #      evidence about this one.
                def model_of(ident):
                    agent = by_id.get(ident) or {}
                    meta = agent.get("metadata") or {}
                    declared = meta.get("model")
                    if declared:
                        return declared
                    kind = agent.get("type")
                    if kind == "claude":
                        found = claude_session_model(agent.get("session-id"))
                        if found:
                            return found
                    if kind == "codex":
                        found = codex_session_model(agent.get("session-id"))
                        if found:
                            return found
                    if kind == "zai":
                        return "glm-5.3 (default)"
                    return None

                def add(ident, kind, state, when, quiet=None):
                    if ident and ident not in seen:
                        seen.add(ident)
                        row = {"id": ident, "type": kind,
                               "status": state, "last-active": when,
                               "model": model_of(ident)}
                        if quiet is not None:
                            row["quiet-sec"] = quiet
                        rows.append(row)

                # Timestamps are ISO-8601 UTC with nanoseconds, which fromisoformat
                # will not take; in that fixed format they compare correctly as
                # plain strings, so compare against a formatted cutoff instead.
                cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.gmtime(time.time() - RECENT_SEC))
                with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                             timeout=2.5) as response:
                    jobs = (json.load(response).get("jobs") or [])

                # Liveness from evidence, not from the agent's `status` flag
                # alone. The flag can outlive a finished dispatched job
                # (claude-10, 2026-09-04: job done 5s after start, flag still
                # "invoking" minutes later), so it reports intent, not
                # liveness. Two kinds of evidence confirm it: a job in a live
                # state (dispatched turns) and a fresh invoke-activity-at
                # timestamp (operator/Emacs turns, which raise no job). Each
                # also says how long since the agent last showed evidence —
                # which a duration on its own cannot, and a long turn and a
                # hung turn must not render the same.
                def iso_epoch(when):
                    try:
                        return time.mktime(time.strptime(when[:19],
                                                         "%Y-%m-%dT%H:%M:%S")) \
                            - time.timezone
                    except (TypeError, ValueError):
                        return None

                live = {}
                for job in jobs:
                    state = str(job.get("state") or "").lower()
                    if state not in ("announced", "queued", "running",
                                     "invoking", "parked"):
                        continue
                    ident = job.get("agent-id")
                    if not ident:
                        continue
                    evs = [e.get("at") or "" for e in (job.get("events") or [])]
                    last = max(evs) if evs else \
                        (job.get("started-at") or job.get("created-at") or "")
                    prev = live.get(ident)
                    if prev is None or last > prev[0]:
                        live[ident] = (last, state)
                now = time.time()

                with urlopen("http://127.0.0.1:7070/api/alpha/agents",
                             timeout=2.5) as response:
                    agents = (json.load(response).get("agents") or {})
                by_id = {}
                for agent in agents.values():
                    ident = (agent.get("id") or {}).get("id/value")
                    if ident:
                        by_id[ident] = agent

                # Every agent that owns a live job is working, whatever its
                # `status` claims in either direction.
                for ident, (last, state) in live.items():
                    t = iso_epoch(last)
                    add(ident, (by_id.get(ident, {}) or {}).get("type"),
                        "invoking", last,
                        quiet=(now - t) if t is not None else None)

                # Operator-driven turns (Emacs seats) raise no job, but their
                # seats report tool activity, and the registry exposes its
                # timestamp as invoke-activity-at. Fresh activity is liveness
                # evidence the job feed cannot see; the registry's own repair
                # treats 120s without it as stale, so we use the same bar.
                for agent in agents.values():
                    ident = (agent.get("id") or {}).get("id/value")
                    if not ident or ident in live \
                            or agent.get("status") != "invoking":
                        continue
                    t = iso_epoch(agent.get("invoke-activity-at") or "")
                    if t is not None and (now - t) <= 120:
                        live[ident] = (agent.get("invoke-activity-at"), "seat")
                        add(ident, agent.get("type"), "invoking",
                            agent.get("invoke-activity-at"),
                            quiet=now - t)

                # A roster `status` of "invoking" is trusted ONLY when a live
                # job or fresh seat activity confirms it. Otherwise the flag is
                # a stale latch (the claude-10 case): the chip must not say
                # "invoking", so the agent degrades to "recent".
                for agent in agents.values():
                    ident = (agent.get("id") or {}).get("id/value")
                    if ident and agent.get("status") == "invoking" \
                            and ident not in live:
                        add(ident, agent.get("type"), "recent",
                            agent.get("last-active") or "")

                for job in jobs:
                    when = job.get("started-at") or ""
                    if when < cutoff:
                        continue
                    ident = job.get("agent-id")
                    state = str(job.get("state") or "").lower()
                    add(ident, (by_id.get(ident, {}) or {}).get("type"),
                        state if state not in TERMINAL_STATES else "recent", when)

                # Agents we have aimed at recently but which the Agency has no
                # live signal for -- the direct-conversation case.
                for ident, when in _recent_targets.items():
                    add(ident, (by_id.get(ident, {}) or {}).get("type"),
                        "recent", when)

                rows.sort(key=lambda r: r["last-active"], reverse=True)
                rows.sort(key=lambda r: r["status"] != "invoking")
                rows = rows[:MAX_ACTIVE]

                # The agent holding the cursor always gets a chip, whatever its
                # status. Without this it vanishes between turns: an agent the
                # operator is talking to reads "idle" the moment it stops replying,
                # and its turns raise no job to be recent about (2026-08-29).
                target = current_target()
                if target and target.get("agent"):
                    if not any(r["id"] == target["agent"] for r in rows):
                        rows.insert(0, {"id": target["agent"],
                                        "type": (by_id.get(target["agent"]) or {}).get("type"),
                                        "status": "idle", "last-active": "",
                                        "model": model_of(target["agent"])})
                self._send(200, json.dumps({"ok": True, "agents": rows,
                                            "target": target}),
                           "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e),
                                            "agents": []}),
                           "application/json")
        elif path == "/agency/procs":
            # The process tree under each invoked agent. The Agency JVM (the
            # process listening on :7070) runs every invoked agent as a direct
            # child -- `claude --print ...` for a Claude seat, `node .../codex
            # exec --json ...` for Codex -- and whatever the agent does (a
            # clojure tick, a bb script, a lake build) hangs below that child.
            # No job or agent record carries a pid, but an agent's
            # `invoke-started-at` matches its child's start time to within a
            # second (2026-09-01: codex-17 17:17:48 <-> pid 994303 17:17:49), so
            # children are labelled by the agent whose invoke began then; the
            # rest are listed as unmatched rather than dropped. Reads /proc via
            # ps only; nothing here talks to the JVM.
            try:
                self._send(200, json.dumps(agency_procs()), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e),
                                            "agents": []}),
                           "application/json")
        elif path == "/topology/status":
            try:
                self._send(200, json.dumps(topology_status()),
                           "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}),
                           "application/json")
        elif path == "/wm/status":
            self._send(200, json.dumps(wm_run_status()), "application/json")

        elif path == "/apm/status":
            # The APM strip: filesystem truth about the frame loop, red when
            # it is stuck at a terminal position (Joe, 2026-09-06).
            try:
                self._send(200, json.dumps(apm_status()), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "error": str(e)}),
                           "application/json")
        elif path == "/agency/backlog":
            try:
                self._send(200, json.dumps(agency_backlog()),
                           "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}),
                           "application/json")
        elif path == "/agency/jobs":
            try:
                with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                             timeout=2.5) as response:
                    payload = json.load(response)
                jobs = []
                for job in payload.get("jobs", []):
                    summary = str(job.get("result-summary") or "")
                    if len(summary) > 120:
                        summary = summary[:117] + "..."
                    jobs.append({key: job.get(key) for key in
                                 ("agent-id", "caller", "state", "started-at",
                                  "finished-at")}
                                | {"result-summary": summary})
                jobs.sort(key=lambda job: (job.get("started-at") or ""),
                          reverse=True)
                self._send(200, json.dumps({"ok": True, "jobs": jobs}),
                           "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e),
                                            "jobs": []}),
                           "application/json")
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
                               "/commentate", "/preview", "/target",
                               "/agency/new"):
            self._send(404, "not found", "text/plain")
            return

        if parsed.path == "/agency/new":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BYTES:
                    raise ValueError("request body length is invalid")
                else:
                    raw = self.rfile.read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                    if not isinstance(payload, dict):
                        raise ValueError("JSON body must be an object")
                    result = create_agent_target(payload.get("type"),
                                                 payload.get("model"))
            except Exception as e:
                result = {"ok": False, "step": "validate",
                          "reason": "invalid request: %s" % e}
            self._send(200, json.dumps(result), "application/json")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(413, json.dumps({"error": "bad length"}), "application/json")
            return

        body = self.rfile.read(length)
        q = parse_qs(parsed.query)

        if parsed.path == "/target":
            try:
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict) or "agent" not in payload:
                    result = {"ok": False, "reason": "JSON object needs agent"}
                else:
                    result = set_target(payload.get("agent"),
                                        payload.get("mode") or "pin")
            except Exception as e:
                result = {"ok": False, "reason": "invalid request: %s" % e}
            self._send(200, json.dumps(result), "application/json")
            return

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
                _t = payload.get("text") or ""
                print("preview: %d words, %d chars: %r" % (len(_t.split()), len(_t), _t[:80]),
                      flush=True)
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
            print("transcribe: %s %.1fs %dms ctx=%s clip=%s%s text=%r" % (
                result.get("model", model), result.get("audio_sec") or 0, result.get("infer_ms") or 0,
                result.get("audio_ctx"), result.get("clip"),
                " looped=%d" % result["looped_segments"] if result.get("looped_segments") else "",
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
