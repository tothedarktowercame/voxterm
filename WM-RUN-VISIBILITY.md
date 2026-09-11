# War Machine run visibility source

Voxterm reads one producer-owned JSON file. An operator-owned
`~/.config/voxterm/wm-source.json` (override: `VOXTERM_WM_SOURCE_CONFIG`)
selects the enacted source with
`{"schema":"voxterm/wm-source-v1","root":"/absolute/visibility/root"}`.
It is reread each poll; malformed selection refuses rather than falling back.
Without that file the legacy source is configured by
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
renders stale and never green. The RUN4 series service now writes the projection from durable controller and
strict evidence readers. Historical admission is not a task-success verdict;
its lifecycle metadata must distinguish the enacted repair from the requested
but not enacted task. Observation age must not be interpreted as worker activity.

The source-selection record may additionally name `queue_status_file`, an
absolute path to a producer-owned `wm/run4-series-queue-visibility-v1` JSON
observation. Queue state is separate from the last run result. Missing queue
configuration means continuous execution is not configured; it never means the
last run did not happen. Held/stopped/stale observations cannot appear running.
Assigned roles and in-flight click identity do not prove current worker activity.
The present queue schema supplies no active-worker evidence; the UI says so.
