"""Regression test: reply-proforma marks are spoken as their intent keywords.

piper's English voices skip the marks, so a paragraph spoken in place of a
missing Gist (voxterm.el, gist-only fallback) lost the cue entirely.

Run: python3 test_speak_marks.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import sanitize_for_speech as say, INTENT_MARKS

CASES = [
    ("㊢ (the change) One branch.", "Report, the change. One branch."),
    ('㊟ ("if there is no gist") It already does.',
     "Qualify, if there is no gist. It already does."),
    ("🈸 Shall I commit?", "Ask action. Shall I commit?"),
    ("㊭: Agreed, do it.", "Propose. Agreed, do it."),
    ("First. ㊩ (tests) Two fail.", "First. Report problem, tests. Two fail."),
    ("No marks here.", "No marks here."),
]

fails = 0
for given, want in CASES:
    got = say(given)
    if got != want:
        fails += 1
        print("FAIL %r\n  want %r\n  got  %r" % (given, want, got))
# Every mark in the key has a reading; none survives into the spoken text.
for mark in INTENT_MARKS:
    if mark in say(mark + " (x) text"):
        fails += 1
        print("FAIL mark survives: %r" % mark)
print("ok" if not fails else "%d failed" % fails)
sys.exit(1 if fails else 0)
