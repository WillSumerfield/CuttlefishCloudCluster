# cli — Glossary

> Local glossary for the `cuttle` tool. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The single front door: one **Python** command, `cuttle`. It is used in three **setup modes** and carries
two kinds of **duty**.

- **Setup mode** — which kind of box `cuttle setup` prepares:
  - **head** — the cluster control plane. **Provisions**.
  - **arm** — a compute node that donates hardware. **Provisions**.
  - **client** — a sign-on-only device that hosts **no Arm** and joins **no Mesh**. It reaches the Head
    over the LAN (or, when remote, over the user's own Tailscale into the LAN) to submit and follow work.
    The minimal mode: it **points** the tool and provisions nothing.
- **Provisioned** — the box carries a *role*: it is a **Head** or an **Arm**, with cluster machinery
  installed on it. The state `teardown` reverses. Only the head and arm **setup modes** reach it, and a
  box that is already there has nothing left to provision.
- **Pointed** — `cuttle` on this box knows *which* **Head** to talk to and trusts that Head's
  certificate. Independent of **Provisioned**: a box can be pointed and roleless (the ordinary
  submitter's laptop), and a **Bundle** can arrive pre-pointed. Being pointed says nothing about
  holding a **Session** — that is **login**, a separate step.
- **Duties** — *host duties* (`setup` / `teardown`) provision the box they run on and are the one thing
  only the CLI can do; *client duties* (login, **Spawn** submission, following work) need only a Head
  address and a **Session**, and run from any machine. (See root glossary: **Clients**.)
- **Wizard** — the guided, interactive form of `cuttle setup`: it asks which **setup mode** to
  provision and then only the questions that mode needs, in the spirit of `zsh-newuser-install`. The
  flags remain the unattended path, since the installer depends on them. `cuttle` is a command-line
  tool throughout — the wizard is a nicer way to answer questions, not a full-screen interface.
- **Bundle** — the hand-carried installer archive `cuttle build-installer` emits: the tool itself, plus
  a way to remove it. It delivers `cuttle` to a box and may leave that box **Pointed** — it never
  **Provisions**, and never decides what the box becomes. Two kinds:
  - **baked** — built *on a **Head***, so it carries that Head's address and certificate and the
    installed box comes out already **Pointed**.
  - **generic** — built anywhere else; the box comes out knowing no Head, and points itself via the
    client **setup mode**.
- **Parity** — the CLI can perform every operation the **Dashboard** offers. Admin operations appear
  **read-only** over the API: every admin *write* is a Head-local command, so a stolen **Session** can
  never mutate admin state. Parity is also what makes the CLI the project's end-to-end **test**
  surface, driven in place of browser testing.

Layout: `core/` — the Python tool. *(A `launcher/` Go layer was once planned but never built, and was
dropped 2026-07-20: cross-platform bootstrap was discarded, and its other duties are either Python
already or moved to the Dashboard. `cuttle` is Python-only.)*
