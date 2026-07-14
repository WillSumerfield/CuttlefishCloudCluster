# karma — Glossary

> Local glossary for the Karma economy. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The economy. Deploys on the **Head**, alongside the Head API — the only thing that knows which **User**
submitted which **Spawn**, and the only identity that *owns* every Spawn in the queue and can therefore
re-rank them.

Karma **ranks Users; it never gates them.** Nothing is refused, held, or killed for want of Karma: a
User with poor standing loses **contention**, and loses nothing at all when nobody is competing.
There is no floor and no bankruptcy — see **Karma**, **Balance**, **Share** in the root glossary.

- **Ledger** — the record of every Karma movement. A **Balance** is a consequence of it, not a
  separate truth.
- **Price** — what a second on a device costs: its **Power** (what it can do) × its **Demand** (how
  badly it's wanted). Capability and scarcity, priced separately.
- **The Tide** — the *forecast* of **Demand**, by hour of day. It does not set the price; it lets a
  User plan around it, and it is what an **Escrow** is quoted from.
- **Escrow** → **Settlement** — Karma is *reserved* when a Spawn queues, and only *moves* when it
  ends.
- **Override** — admin add/remove/set of a User's **Balance**. Changes standing, and is felt in every
  future contention.
- **Queue bump** — admin shove of one queued Spawn up the queue. Bypasses the economy entirely and
  touches no Balance. Deliberately *not* an **Override**.

Provisioned via `setup/`.
