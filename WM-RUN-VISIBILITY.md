# War Machine run visibility source

Voxterm reads one producer-owned JSON file, configured by
`VOXTERM_WM_RUN_ROOT` and `VOXTERM_WM_RUN_STATUS_FILE` (default
`run-visibility.json`). It never writes this file and never treats readiness,
preparation documents, or Agency presence as run evidence.

The required `wm/run-visibility-v1` record has `run_id`, `stage`, `result`,
`updated_at`, and a nonempty `trials` array. Each trial requires `trial_id`,
`stage`, `result`, and `updated_at`; `worker`, `reviewer`, and
`blocked_reason` are displayed only when the producer records them. Timestamps
are RFC 3339 with a timezone. Stages are `planned`, `dispatched`, `working`,
`review`, `blocked`, `failed`, `complete`, or `accepted`; results are
`pending`, `passed`, `failed`, or `blocked`.

Missing evidence renders **no run evidence** and explicitly says trial detail
is absent. Malformed evidence renders an alarm. Evidence older than
`VOXTERM_WM_STALE_S` (900 seconds by default), including any stale trial,
renders stale and never green. The future RUN4 runner must atomically write
this projection from the joined sources identified by the runner audit:
serving `GET /api/alpha/wm/click` status for current liveness,
`futon2/data/wm-full-loop/<cohort>/attempt-NNN/` checkpoint events, and a
verified `futon3c/data/wm-click-run-bindings/click-run-binding-<click-id>.edn`
record. Terminal display additionally requires the durable closed checkpoint
and matching binding. Phase-log age may establish last activity, but cannot
establish continued execution. No producer currently joins those facts to
RUN4 series and trial identities, so it remains an integration dependency.
