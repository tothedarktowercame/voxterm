# WM queue reader deployed, queue not activated

Codex-17 deployed 6644fc9 by restarting only voxterm.service on 2026-09-11.
The shared Futon3c JVM and APM execution were not restarted.

Validation: test_wm_runs.py 20 checks pass; queue reader 2 tests pass;
Python compilation passes. Actual GET /wm/status reports:
- run evidence present for run4-repair058-admission-20260911-v1;
- historical execution completed;
- active_workers [];
- queue state unconfigured, configured false.

Existing ~/.config/voxterm/wm-source.json continues to point at the real058
visibility root. Installing a reviewed queue later must add its exact producer
queue_status_file there; the reader rereads this selection each poll. No queue
state, capacity, activation or fabricated worker activity was created here.
