# telemetry/setup

No code of its own. W&B env injection lives in `head/api/src/ccc_head_api/spawns.py`; the Head-box
health monitor (disk, process-liveness, rogue GPU/CPU usage, SMTP alerting) is
`cli/core/src/cuttle/monitor.py`, installed by `cuttle setup --head`.

The monitor watches the Head's box. There is no per-Arm telemetry: CCC does not watch donors'
machines.
