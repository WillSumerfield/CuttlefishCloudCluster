# karma/setup

**The Karma economy has no code of its own here.** It ships inside the Head API's package, and this
note exists to say why — and where to look instead.

| what | where |
| --- | --- |
| Balance, Decay, Share, Override | `head/api/src/ccc_head_api/karma.py` |
| the tick (`ccc-karma.service`) — pushes nice onto the queue | `head/api/src/ccc_head_api/tick.py` |
| the ledger + escrow tables (`head.db`) | `head/api/src/ccc_head_api/db.py` |
| `GET /karma` (standings, ledger, demand, usage — read-only) | `head/api/src/ccc_head_api/main.py` |
| `cuttle karma` (standings), `override` (→ `ccc_head_api.admin`), `bump` (direct `scontrol`) — all Head-local, no API write route | `cli/core/src/cuttle/karma.py` |
| provisioning (the unit) | `cli/core/src/cuttle/headapi.py` → `install_karma` |

The economy is not a separate deployable: it reads the **Attribution** ledger to learn who submitted a
Spawn, writes the Karma ledger in the same `head.db`, and prices Arms from the same registry — a
service split from the Head API would need all three anyway, and would only add a second copy of them.
What *is* split is the **tick**: it runs as its own systemd service off the same venv, so the economy
cannot wedge the API, nor the API the economy.

There is no FairShare sync and no accounting DB. We set each Spawn's priority ourselves, via `nice` —
which needs no privilege at all, because every Spawn runs as the **Mantle** and the Head therefore
*owns* every job in the queue.
