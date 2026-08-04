# gateway — Glossary

> Local glossary for the Gateway. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The authenticated network entryway and cluster fabric.

- **Mesh** — WireGuard mesh (Tailscale / self-hosted Headscale) putting every node on one flat
  encrypted network with NAT traversal, so remote Arms reach the Head like a LAN.
- **Sign-on** — built-in username/password auth on the Head (own user table, argon2 + Sessions) that
  gates human access + job submission. No external IdP. Distinct from the **Mesh**, which authorizes
  machines, not people (see root glossary: User, Session).

Provisioned via `setup/`.
