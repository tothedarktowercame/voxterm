# 2026-09-10 17:33 watchdog halt fixture

`coordinator.edn.watchdog.edn` is the leading 2367 bytes, verbatim, of
`/home/joe/code/futon3c/data/apm-campaigns/jit-all-open-v3/coordinator.edn.watchdog.edn`
as it stood between the halt (17:33:53Z) and the resume (18:35Z), followed by
`}}}` to close `:state`, `:watchdog/durable-stop` and the top-level map. The
resumed watchdog rewrote the live file at 18:36:39Z, so the bytes were
recovered from claude-5's session transcript, where a `head -c 2500` of the
live file was captured at 18:3x; the cut falls after
`:regulator/reconciliation :quiescent`, inside the durable stop's
`:regulator/quiescence-history`, which the strip does not read.

It carries every field the strip reads: `:watchdog/status :halted`,
`:watchdog/observed-at-ms 1789061633484`, `:watchdog/halt-reason {:code
:external-job-deadline-exceeded ...}`, the durable stop's
`:regulator/last-result {:ok false, :error/code
:problem-queue-state-plan-mismatch}` and the trace flags.

The campaign and frame files come from `../apm-cascade-strip/`. The
`:watching` case is a planted mutation in the test itself.
