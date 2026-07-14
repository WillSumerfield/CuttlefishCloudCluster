# karma/setup

**The Karma economy has no code of its own here.** It ships inside the Head API's package, and this
note exists to say why — and where to look instead.

| what | where |
| --- | --- |
| Balance, Decay, Share, Override | `head/api/src/ccc_head_api/karma.py` |
| the tick (`ccc-karma.service`) — pushes nice onto the queue | `head/api/src/ccc_head_api/tick.py` |
| the ledger + escrow tables (`head.db` v3) | `head/api/src/ccc_head_api/db.py` |
| `GET /karma`, `POST /karma/override` | `head/api/src/ccc_head_api/main.py` |
| `cuttle karma` / `override` / `bump` | `cli/core/src/cuttle/karma.py` |
| provisioning (the unit) | `cli/core/src/cuttle/headapi.py` → `install_karma` |

The economy is not a separate deployable: it reads the **Attribution** ledger to learn who submitted a
Spawn, writes the Karma ledger in the same `head.db`, and prices Arms from the same registry — a
service split from the Head API would need all three anyway, and would only add a second copy of them.
What *is* split is the **tick**: it runs as its own systemd service off the same venv, so the economy
cannot wedge the API, nor the API the economy.

There is no FairShare sync and no accounting DB (the pre-grill sketch had both). We set each Spawn's
priority ourselves, via `nice` — which needs no privilege at all, because every Spawn runs as the
**Mantle** and the Head therefore *owns* every job in the queue. See `temp/KARMA.md`.
