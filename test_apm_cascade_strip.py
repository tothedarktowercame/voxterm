"""Regression test: a partial cascade is never reported as an empty shelf.

f193 (2026-09-08) served 313 memory ids and failed only the expansion over
them, while the strip read "frame is running WITHOUT served memory" -- a
degraded success rendered as a total absence, which sent the diagnosis to
the wrong layer. The empty shelf is a different fault (f51/A10) and must
stay loud, so this checks both directions plus the two cases in between.

Drives the real apm_status() over a temp copy of f193's own files rather
than re-implementing the branch: a test that reads a copy of the logic is
the same mistake the strip made.

Run: python3 test_apm_cascade_strip.py
"""
import os, re, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

SRC_C = ("/home/joe/code/futon3c/data/apm-campaigns/jit-all-open-v3")
CAMP = "jit-all-open-v3"
SRC_F = os.path.join(SRC_C, CAMP + "-f193")

root = tempfile.mkdtemp(prefix="s3-strip-")
cdir = os.path.join(root, CAMP)
fdir = os.path.join(cdir, CAMP + "-f193")
os.makedirs(os.path.join(fdir, "live"))
for f in os.listdir(SRC_C):
    p = os.path.join(SRC_C, f)
    if os.path.isfile(p):
        shutil.copy2(p, cdir)
for f in os.listdir(SRC_F):
    p = os.path.join(SRC_F, f)
    if os.path.isfile(p):
        shutil.copy2(p, fdir)
COP = os.path.join(fdir, "live", "memory-cascade-operation.edn")
shutil.copy2(os.path.join(SRC_F, "live", "memory-cascade-operation.edn"), COP)
server.APM_ROOT = root

real = open(COP).read()
fails = []

def run(label, text, want_in=None, want_not_in=(), want_state=None):
    open(COP, "w").write(text)
    d = server.apm_status()
    st, al = d.get("state"), d.get("alert") or ""
    print("%-18s %-9s | %s" % (label, st, al))
    if want_in and want_in not in al:
        fails.append("%s: missing %r in %r" % (label, want_in, al))
    for w in want_not_in:
        if w in al:
            fails.append("%s: still says %r" % (label, w))
    if want_state and st != want_state:
        fails.append("%s: state %r not %r" % (label, st, want_state))

run("f193 real", real, want_in="313 seeds served",
    want_not_in=("WITHOUT served memory",), want_state="degraded")
run("empty shelf", real.replace(":seed-count 313", ":seed-count 0"),
    want_in="WITHOUT served memory", want_state="degraded")
run("unknown count", re.sub(r":seed-count \d+", ":stage :expanding", real),
    want_in="served memory unknown",
    want_not_in=("WITHOUT served memory", "seeds served"))
run("succeeded", real.replace(":status :failed", ":status :succeeded"),
    want_not_in=("cascade",))

shutil.rmtree(root, ignore_errors=True)
print()
if fails:
    print("FAIL:"); [print("  -", f) for f in fails]; sys.exit(1)
print("all four directions pass against the real apm_status()")
