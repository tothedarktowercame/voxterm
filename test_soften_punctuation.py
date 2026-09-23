"""Regression test: whisper's invented sentence breaks are removed, and the
capitals that only exist because of them are lowered.

Joe dictates and types into the same buffers. Whisper punctuates dictation as
written prose and takes its sentence boundaries from the pauses, so one spoken
clause comes back as "Turn that I'm... Sending now." -- two invented stops and
a capital mid-sentence. The cases below are all real transcripts from the
2026-09-23 session.

What must NOT change: a genuine final stop, ordinary commas, and words that are
capitalised for their own sake (I, Claude, Kimi, Zai).

Run: python3 test_soften_punctuation.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import soften_punctuation as soften

CASES = [
    # invented break inside one clause, marked by a pause
    ("Turn that I'm... Sending now.", "Turn that I'm sending now."),
    # dictation of a list, fragmented into three "sentences"
    ("The translations should be looking at thee. Affective. Words.",
     "The translations should be looking at thee affective words."),
    # proper nouns keep their capitals across a removed break
    ("And we hand them to Kimi. Then, you know. We cut through a lot of noise.",
     "And we hand them to Kimi then, you know we cut through a lot of noise."),
    ("I think what we should do is ask Zai. It occurs to me that this is hard.",
     "I think what we should do is ask Zai it occurs to me that this is hard."),
    # a trailing ellipsis becomes the final stop rather than vanishing
    ("We can deal with it when it gets back. My point is that ultimately...",
     "We can deal with it when it gets back my point is that ultimately."),
    # nothing to do: no break, no ellipsis
    ("Yeah, that's right.", "Yeah, that's right."),
    # "I" is never lowered
    ("That is the plan. I will follow it.", "That is the plan I will follow it."),
]


def main():
    bad = 0
    for given, want in CASES:
        got = soften(given)
        if got != want:
            bad += 1
            print(f"FAIL  {given!r}\n  want {want!r}\n  got  {got!r}")
    print(f"{len(CASES) - bad}/{len(CASES)} cases pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
