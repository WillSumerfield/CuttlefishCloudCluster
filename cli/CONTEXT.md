# cli — Glossary

> Local glossary for the `cuttle` tool. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The single front door: one **Python** command, `cuttle`. It is used in three **setup modes** and carries
two kinds of **duty**.

- **Setup mode** — which kind of box `cuttle setup` provisions:
  - **head** — the cluster control plane.
  - **arm** — a compute node that donates hardware.
  - **client** — a sign-on-only device that hosts **no Arm** and joins **no Mesh**. It reaches the Head
    over the LAN (or, when remote, over the user's own Tailscale into the LAN) to submit and follow work.
    The minimal mode: it installs only the tool itself, nothing a compute node needs.
- **Duties** — *host duties* (`setup` / `teardown`) provision the box they run on and are the one thing
  only the CLI can do; *client duties* (login, **Spawn** submission, following work) need only a Head
  address and a **Session**, and run from any machine. (See root glossary: **Clients**.)
- **Wizard** — the guided, interactive form of `cuttle setup`: it asks which **setup mode** to
  provision and then only the questions that mode needs, in the spirit of `zsh-newuser-install`. The
  flags remain the unattended path, since the installer depends on them. `cuttle` is a command-line
  tool throughout — the wizard is a nicer way to answer questions, not a full-screen interface.
- **Parity** — the CLI can perform every operation the **Dashboard** offers. Admin operations appear
  **read-only** over the API: every admin *write* is a Head-local command, so a stolen **Session** can
  never mutate admin state. Parity is also what makes the CLI the project's end-to-end **test**
  surface, driven in place of browser testing.

Layout: `core/` — the Python tool. *(A `launcher/` Go layer was once planned but never built, and was
dropped 2026-07-20: cross-platform bootstrap was discarded, and its other duties are either Python
already or moved to the Dashboard. `cuttle` is Python-only.)*
