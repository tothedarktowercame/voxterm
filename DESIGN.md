# voxterm as a futon3c surface

Design note. Nothing here is implemented yet.

## The decomposition

Two separate things, in two separate layers:

| | What it is | Where it lives |
|---|---|---|
| **voxterm** | A **surface** — voice in, voice out. Transport and rendering. | Surface layer, alongside `"irc"`, `"emacs-socket"`, `"ws"` |
| **`:converse`** | A **peripheral** — a constrained capability envelope for talking about code without editing it. | `futon3c/resources/peripherals.edn` |

The registry settles this. Every entry in `peripherals.edn` is `{:peripheral/id :tools :scope :entry :exit :context}` — the schema has **no slot for transport, rendering, or output medium**, because a peripheral constrains *what the agent can do*. voxterm constrains nothing; it changes how words get in and out. Putting it in `peripherals.edn` would be the wrong layer.

What we want is cross-cutting: one session whose *peripheral* is working and whose *surface* is spoken. That's fine — it just means the voice half is surface-layer config, not a peripheral entry.

## Surface contract

`peripheral-spec.md` says constraints are **"structural, not behavioral — no 'please be careful' compliance theater"**, and `CLAUDE.md` lists "keep responses short" as a capability restriction (bad) versus surface context (good). So the contract states facts and lets brevity follow:

```
Runtime surface contract:
- Current surface: voxterm (spoken).
- The user's message was dictated and transcribed by whisper small.en;
  proper nouns, identifiers and symbols may be mangled. Ask rather than
  guess when a token looks wrong.
- Your reply will be synthesised to speech and played on the user's phone.
  It is heard, not read: code blocks, tables, and URLs do not survive.
- The user is at a DeX desk and may not be looking at the screen.
- Work continues in the Emacs REPL buffer; the spoken channel is a summary
  of it, not a replacement for it.
```

Constructed the same way as `codex-repl--surface-contract` (`codex-repl.el:3528`) — a list of factual statements, joined.

Note the third bullet does double duty: it's the reason brevity happens *and* the reason the agent won't read a diff aloud.

## The `:converse` peripheral

Read-only tools, so "we're just talking" is structural rather than hoped-for:

```clojure
:converse
{:peripheral/id :converse
 :peripheral/tools #{:read :glob :grep :bash-readonly}
 :peripheral/scope :full-codebase
 :peripheral/entry #{:user-request :from-explore}
 :peripheral/exit #{:ready-to-edit :user-request :hop-edit :hop-explore}
 :peripheral/context {:session-id :inherit}}
```

Hopping `:converse → :edit` on "go do it" carries session-id, per the hop protocol. **Unverified:** this must validate against `futon3c.social.shapes/PeripheralSpec`, which I have not read — the `:entry`/`:exit` keywords may need registering there first.

`:converse` is optional. The spoken surface works over a session that is already in `:edit`; the peripheral only buys the structural guarantee that the agent can't edit while conversing.

## Return path

Input is done (whisper → `/transcribe` → `emacsclient` → `claude-repl-send-input`). The missing half is getting the reply back out.

`claude-repl-send-input` passes `:on-response` into `agent-chat-send-input` (`claude-repl.el:1238`). That's the seam:

```
agent responds → :on-response hook → POST /speak → piper → page plays it
```

The page already holds an audio context. Simplest delivery is polling `/speak/next`; SSE is doable on `http.server` if polling feels laggy.

## The spoken block is part of the turn — no second model

**The agent appends a short spoken summary as the last block of its turn.** That's all. No condensation model, no local GLM, no API round trip.

This is structurally the same as replying on IRC or in an Emacs buffer: the surface contract tells the agent where its output lands, and it writes accordingly. Nothing about voice is special enough to need a different mechanism than the two surfaces already in use.

Why this beats running a summariser over the buffer:

- **A summariser produces another model's account of what happened. The agent's own closing line is what it meant to say.** For a thinking partner that distinction is the whole point — condensation reliably loses intent, emphasis, and the "here's the bit you actually care about" judgement that makes the reply worth hearing.
- No extra round trip, no second dependency, no API key in the loop.
- Nothing is lost on latency. A summariser would also run after the turn completes, so end-of-turn is end-of-turn either way.

An earlier draft of this note proposed Haiku 4.5 as a fallback when the marker was absent. That's defensive scaffolding against the agent not following its own surface contract — the same thing the contract exists to handle for IRC. Drop it.

**What this does not cover: mid-turn progress.** If the agent works for three minutes, the user hears nothing until it finishes. That's a separate feature (a spoken acknowledgement at dispatch, or speaking the first sentence off the stream), not an argument for a condensation model.

### Marker convention — the real open question

The turn now has two audiences: the buffer gets the full working trace, the last block gets spoken. `/speak` needs to extract that block unambiguously, and it has to read naturally in a buffer a human is watching live.

**Nothing existing to reuse.** `claude-repl` splits turns on blank lines (`claude-repl.el:1286`, `:1316`) and has no structured-block parser — so this is new design, and it should probably be added to `claude-repl-mode`'s rendering rather than bolted onto the extraction side.

Constraints for whatever gets chosen:

- Unambiguous to extract server-side without a real parser
- Reads as a normal part of the reply to someone watching the buffer, not as protocol noise
- Survives being absent — a turn with no spoken block should degrade to silence, not to speaking the whole buffer

## The voice channel is a pure duplicate

The REPL buffer streams exactly as it does today; speech is a second rendering of
the same text, never a replacement and never in the path. Nothing waits on audio.

This is the load-bearing property of the whole design:

- Speech can be wrong, ugly, truncated, lagging, or entirely absent and the
  session is unaffected.
- Skipping an unspeakable paragraph (code fence, table) costs nothing — the
  screen already has it.
- The sanitizer only has to be *not embarrassing*; every failure degrades to
  silence.
- Therefore: ship simple and tune from use. Length caps, sanitizer coverage, and
  turn-start-vs-end are all knobs to set by ear, not problems to solve up front.

## TTS

Piper, already installed, `en_GB-semaine-medium`. Measured on this box:

**0.77 s wall to synthesise 8.61 s of audio — ~11× realtime.**

Full loop: ~0.5 s whisper → agent thinking → ~0.8 s piper. The agent's own latency dominates, which is the right shape — and with no condensation hop there is nothing between the turn ending and speech starting.

## Open questions

- **Barge-in.** Interrupting mid-speech needs the listener live during playback with echo cancellation. This is what separates "works" from "feels like the app", and it isn't solved by any of the above.
- **`PeripheralSpec` conformance** for `:converse` — unread.
- **Where the surface contract is injected.** `codex-repl.el` builds its contract in elisp at prompt-construction time; whether voxterm's belongs there or server-side depends on where the claude-repl prompt is assembled, which I haven't traced.
- **Marker convention** for the spoken block — see above; the one piece of genuinely new design here.
- **Turn-start vs turn-end vs both.** claude.ai's voice mode feels instant because no work happens between hearing and answering; a coding turn takes minutes of tool calls, so the question isn't how to be instant, it's what the silence should sound like.

  **Rejected: echoing the transcript back at dispatch.** Built and tried (2026-08-12); it reads as a hyperactive parrot, not a thinking partner. The justification was that it confirms the transcription before minutes of wrong work — but the transcript is already on screen in the pending buffer and the segment log, which is the better channel for checking it: scannable, skippable, no time cost. This is the same "don't duplicate what the monitor already shows" argument that rules out mid-turn narration; it applies here too. The `/speak` endpoint remains as the TTS path for real spoken output; the echo survives only as a debug toggle, off by default.

  If a turn-start signal is wanted, the two candidates are a **non-verbal tone** (confirms "heard you, dispatching" without words — the content check stays visual) or an **agent-generated first sentence** off the response stream, which is what claude.ai-like actually means: the agent's first thought, not a repetition of the user's.

  **Silence during the working phase is probably correct**, for a reason specific to this setup: at the DeX desk the monitor already shows the trace. Speech duplicating a channel the user can already see is noise. Voice earns its place at the turn boundaries, when they're *not* looking at the screen.

  Default to ack + end summary, nothing mid-turn. Both are a line each in the page — make them toggles and settle it with a day of use rather than argument.
