# gateway — Glossary

> Local glossary for the Gateway. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The authenticated network entryway and cluster fabric.

- **Mesh** — WireGuard mesh (Tailscale / self-hosted Headscale) putting every node on one flat
  encrypted network with NAT traversal, so remote Arms reach the Head like a LAN.
- **SSO** — Imperial OIDC/SSO gating node enrolment and human access (auth layer on top of the mesh).

Provisioned via `setup/`.
