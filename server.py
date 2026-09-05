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


def env_list(name, default):
    """Read a comma-separated VOXTERM setting, preserving configured order."""
    return [item.strip() for item in os.environ.get(name, default).split(",")
            if item.strip()]


CODEX_MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def codex_models(limit=4):
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
    by_session, starts, info = {}, [], {}
    with urlopen("http://127.0.0.1:7070/api/alpha/agents", timeout=2.5) as r:
        for agent in (json.load(r).get("agents") or {}).values():
            ident = (agent.get("id") or {}).get("id/value")
            if not ident:
                continue
            info[ident] = (agent.get("status"), agent.get("invoke-activity"))
            if agent.get("session-id"):
                by_session[agent["session-id"]] = ident
            t = epoch(agent.get("invoke-started-at"))
            if t:
                starts.append((t, ident))
    try:
        with urlopen("http://127.0.0.1:7070/api/alpha/invoke/jobs",
                     timeout=2.5) as r:
            for job in (json.load(r).get("jobs") or []):
                if job.get("state") in ("running", "queued"):
                    t = epoch(job.get("started-at") or job.get("created-at"))
                    if t and job.get("agent-id"):
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

    # Attribution for unmatched JVM children. A seat process persists long
    # after its job finished, so job created-at correlation decays as soon as
    # that job ages out of the endpoint's rolling 20-job window (claude-10's
    # seat, 2026-09-04: attributable at rehearsal, "?" again 30 minutes
    # later). The durable key is the REGISTRY: an agent's `registered-at` is
    # stamped once, never expires, and the seat process starts within seconds
    # of it. Job created-at and invoke-started-at stay as additional
    # candidates; this claims likely ownership without consuming the `used`
    # set -- it names the seat, it does not assert a running turn. (Claude
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

    def likely_owner(child_start):
        best = None
        for t, cand in attr_candidates:
            d = child_start - t
            if -5 <= d <= 20 and (best is None or abs(d) < best[0]):
                best = (abs(d), cand)
        return best[1] if best else None

    _SESSION_OWNERS.update(by_session)

    def owner_of_session(sid):
        """The agent a `--resume <sid>` seat belongs to, registry first."""
        return by_session.get(sid) or _SESSION_OWNERS.get(sid)

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
            cands = []
            for cand in info:
                toks = [t for t in cand.lower().split("-") if len(t) >= 3]
                if toks and all(t in path for t in toks):
                    cands.append((len(cand), cand))
            if cands:
                ident, how = max(cands)[1], "cwd"
        if not ident and sole_role and c["elapsed"] < 120 and not sid:
            ident, how = sole_role, "sole-role-job"
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
            rows.append({"id": ident, "status": "seat", "activity": act,
                         "matched-by": how, "pid": child,
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
        elif path == "/agency/runtimes":
            runtimes = []
            for runtime, spec in AGENT_RUNTIMES.items():
                runtimes.append({"type": runtime, "label": spec["label"],
                                 "models": spec["models"],
                                 "model-prefix": spec["model-prefix"]})
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
                def model_of(ident):
                    meta = (by_id.get(ident) or {}).get("metadata") or {}
                    return meta.get("model")

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
