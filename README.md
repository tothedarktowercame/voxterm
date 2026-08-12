# voxterm

Push-to-talk voice input: browser mic → 16 kHz mono WAV → whisper.cpp on this box → text.
Stdlib Python only, no venv, no ffmpeg (the page encodes WAV client-side).

## Run

```sh
cd ~/code/voxterm && python3 server.py          # binds 127.0.0.1:8081
VOXTERM_THREADS=16 VOXTERM_PORT=9000 python3 server.py   # knobs
```

## Reach it from the phone

The page needs a **secure context** for `getUserMedia`. `localhost` counts as one, so
an ssh tunnel avoids TLS entirely:

```sh
# in Termux, a SEPARATE session — mosh cannot forward ports
ssh -N -L 8081:localhost:8081 joe@<box>
```

Then open `http://localhost:8081` in Chrome on the phone. "Add to Home Screen" for an
app-like launcher. The server stays bound to loopback, so nothing is exposed.

Hold the big button to talk, or hold **space** (works with a physical keyboard in DeX).

## Vocabulary — getting "Claude" instead of "quad"

small.en mangles domain words. whisper-cli's `--prompt` biases the decoder, and
**how you write the prompt matters more than what's in it.** Measured on this box:

| prompt style | "Ask Claude…" | "Tell Claude…" | jargon |
|---|---|---|---|
| none | "Ask **call**…" | ✓ | FUTUM-3C, Clodjoe, M-RAPL |
| word list (`Claude, Emacs, Clojure, …`) | "Ask **call**…" | "Tell **Clojure**…" | ✓ |
| **words in position** (default) | ✓ | ✓ | ✓ |

A bare list is actively harmful for `Claude`: listing it next to `Clojure` makes
the two compete and *breaks* a case that worked with no prompt at all. Showing
each word where it actually occurs — `"Ask Claude. Tell Claude. Claude Code."` —
fixes every case, at no latency cost (~550 ms either way).

Override with `VOXTERM_PROMPT`. Keep the positional style; extend it with your
own project names as they come up.

`FIXUPS` in `server.py` is the backstop for residue (`quad` → `Claude`,
`futon 3 c` → `futon3c`, `em axe` → `Emacs`). Keep it small and unambiguous —
`call` and `Clojure` are also common mishearings of "Claude" but are real words
here, so they must never be substituted. Word-boundary anchored, so
"quadratic" survives.

## Always-listening mode

The 🚀 button replaces push-to-talk with a continuous listener:

1. On enable it calibrates the noise floor for ~1 s — stay quiet.
2. An energy gate opens a segment ~128 ms after you start talking (with ~384 ms of
   pre-roll so the first word isn't clipped) and closes it after ~700 ms of silence.
   Silence runs no inference at all, so idle CPU is zero.
3. Each segment is transcribed and appended to a **pending buffer**.
4. Saying **"rocket"** dispatches the buffer and clears it.

The trigger is tail-anchored: it only fires when the keyword ends the utterance
(one trailing word tolerated). That way "the rocket ship launched" and "I rocked up
late" don't send. Homophones `rock it` / `rockit` / `rocked` are all accepted, since
whisper produces them.

Segments are POSTed strictly in order, so buffered text stays coherent even when you
talk faster than inference. `navigator.wakeLock` holds the screen on while listening.
Whisper's stock near-silence hallucinations ("Thank you.", "Thanks for watching!")
are dropped rather than buffered.

Tuning: the meter shows live level with the threshold as a vertical marker. If it
triggers on room noise, raise **sensitivity**; if it clips your first syllable, lower
it. **Recalibrate** re-samples the noise floor.

## Measured on this box (6.0 s utterance, 12 threads, EPYC 4545P, CPU only)

| model | audio-ctx | decode | infer | notes |
|---|---|---|---|---|
| small.en | 256 | greedy | **492 ms** | fastest; drops some content words |
| small.en | 512 | greedy | **559 ms** | default — same text as full ctx |
| small.en | 768 | greedy | 787 ms | |
| small.en | full | greedy | 1513 ms | |
| small.en | full | beam 5 | 1619 ms | beam buys nothing here |
| small.en | 128 | greedy | 1949 ms | breaks — output truncates |
| large-v3-turbo | full | greedy | 6663 ms | best text, too slow to talk to |
| large-v3-turbo | 256 | greedy | 10995 ms | **worse** — see below |

Two counterintuitive results:

- **`-ac` helps small.en 3× but actively hurts large-v3-turbo.** Turbo with a clipped
  audio context falls into a repetition loop ("Research Research process, the research"),
  which triggers decoder fallbacks and costs *more* than full context. Use `-ac` with
  small models only.
- **`-ac 128` is past the cliff** — it doesn't just degrade, it truncates and gets slower.

## Routing

Emacs needs no listener — `emacsclient` pushes into the running daemon over its unix
socket. Dispatched text POSTs to `/route`, which shells out to the chosen sink.

```sh
VOXTERM_SINK=emacs                  # emacs | tmux | none  (UI selector overrides)
VOXTERM_EMACS_SOCKET=server         # matches `emacsclient -s server`
VOXTERM_TMUX_TARGET=main:1
VOXTERM_TMUX_ENTER=0                # 1 to press Enter after sending
VOXTERM_EMACS_SUBMIT=1              # press RET after inserting (UI checkbox overrides)
```

**Submitting.** With submit on, `voxterm-insert` runs whatever `RET` is bound to in
the target buffer — in `claude-repl-mode` that is `claude-repl-send-input`, so
dictation actually sends rather than just landing in the input area. It calls the
command rather than synthesising a keypress, which is more reliable from a daemon
eval. In an ordinary buffer RET inserts a newline, as you'd expect.

**The tmux sink deliberately ignores the submit checkbox** and stays gated behind
`VOXTERM_TMUX_ENTER`. Auto-pressing Enter there would execute whatever whisper
happened to transcribe as a shell command.

`voxterm.el` is loaded on demand by the server, so there is nothing to add to
`init.el`. `voxterm-insert` puts text at point in the selected window of a visible
frame; if that buffer is read-only or a minibuffer it appends to `*voxterm*` instead.

Check where dictation would land before trusting it:

```sh
emacsclient -s server -e '(progn (load "~/code/voxterm/voxterm.el" t t) (voxterm-target-name))'
```

To auto-submit into a REPL, add to `voxterm-after-insert-hook` — it runs in the target
buffer after the insert.

In push-to-talk mode each utterance routes on release. In always-listening mode text
buffers until "rocket".

## Instant reply — a thinking partner alongside the worker

Tickbox **instant reply (Opus)**. On dispatch, the utterance goes to the agent in
Emacs *and*, in parallel, to a fast model that says the most useful thing it can
right now — likely cause, what to check first, the caveat that will bite, or a
direct answer. You hear a considered thought within a couple of seconds while
the deeper agent is still working.

It is a commentator, not a worker: no tools, and it never touches the coding
path. The one guardrail is narrow — **it reasons, it does not report.** It may be
wrong about the problem (cheap: the buffer on screen carries the truth, and a
wrong idea is still a thought worth having), but it must not claim to have looked
at anything or announce what the agent is about to do, because those are claims
you cannot check.

It sees the **tail of whichever buffer you are focused on** (`VOXTERM_CONTEXT_CHARS`,
default 6000, `0` disables), fetched read-only through `voxterm-context` over
`emacsclient`. Same target-window logic as dictation, so text goes where you are
looking and context comes from where you are looking.

```sh
export ANTHROPIC_API_KEY=sk-ant-...      # required; nothing else needs it
./.venv/bin/python server.py             # SDK lives in the venv
VOXTERM_COMMENTATOR_MODEL=claude-haiku-4-5   # cheaper alternative
```

Roughly $0.01 per utterance on Opus with 6 k chars of context. Thinking is off at
effort `low` because latency is the whole point; that is safe here because the
model is given no tools.

Without a key the tickbox reports `set ANTHROPIC_API_KEY` and everything else
carries on — a failure is a quiet turn.

## Speaking the agent's replies

The first paragraph of a streamed reply is the agent's orientation for the turn.
Speaking it needs no cooperation from the agent and no surface contract —
paragraph breaks already exist, and `claude-repl` already splits on them.

```
agent-chat-stream-text  ──advice──▶  POST /say   (server sanitises + queues)
                                          │
        page polls GET /say/next  ◀────────┘  ──▶ POST /speak ──▶ plays
```

The box has no speaker, so Emacs cannot just synthesise — it enqueues, and the
page (on the phone) drains. Turn it on inside Emacs:

```
M-x voxterm-toggle-speaking
```

Off by default, so a session you aren't listening to stays silent. It advises
`agent-chat-stream-text` and `agent-chat-end-streaming-message` from
`voxterm.el` — **nothing in futon3c is modified**, and toggling off removes the
advice. The end-of-stream hook flushes single-paragraph replies that never
contain a blank line.

**The audio is a pure duplicate of the buffer.** The REPL streams exactly as it
does today; speech is never in the path. So every failure degrades to silence:
a paragraph opening with a code fence, table, heading or blockquote is skipped,
a sanitiser miss is at worst ugly, and a dead server just means a quiet turn.
The queue is capped at 8 (`MAX_SAY_QUEUE`) and drops oldest first — stale speech
is worse than dropped speech.

Sanitiser: strips inline code, `**bold**`, markdown links, bare URLs, leading
list markers; collapses `/long/paths/to/file.py` to the basename. Anything left
without two consecutive letters is dropped.

| endpoint | who calls it |
|---|---|
| `POST /say` | Emacs — enqueue text (sanitised server-side) |
| `GET /say/next` | the page — poll, ~1.2 s |
| `POST /speak` | the page — text in, WAV out (`X-Synth-Ms` header) |
