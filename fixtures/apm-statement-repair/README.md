# f216 statement-repair fixture

Captured 2026-09-10 ~17:00Z while the Guide repair of m03J02 was running.

- `ledger.edn`: verbatim copy of
  `/home/joe/code/futon3c/data/apm-campaigns/jit-all-open-v3/jit-all-open-v3-f216/ledger.edn`
  (last event `:frame/stopped`, `:reason :statement-refuted`,
  `:failed-invariants [:statement-refuted-by-solver]`); SHA-256 prefix `74cd22a21ba7c62c`.
- `queue-state.edn`: the live `queue-state.edn` is 741 KB of frame history, so
  only its top-level `:status`, `:next-index`, `:frame-ordinal`, `:queue/id`
  and the complete `:statement-repair/handoff` were kept, printed by the
  futon3c JVM's own `pr-str` (values verbatim), with `:parked []` added so
  the park projection has a collection to read.

The coordinator used for campaign discovery is the f193 fixture's copy in
`../apm-cascade-strip/`. Mutated cases are planted in the test itself.
