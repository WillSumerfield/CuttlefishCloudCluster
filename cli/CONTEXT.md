# cli — Glossary

> Local glossary for the `cuttle` tool. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details.

The single cross-platform front door (`cuttle`). Two layers matching where code runs:

- **Launcher** — the host-native Go binary. Runs on the raw OS; bootstraps the Linux layer
  (native / WSL2 / VM), joins the mesh, registers the Retract hotkey, drives stage-in/out.
- **Core** — the Python (`typer`) layer inside the Linux layer: recipes, Spawn submission,
  Tide Pool sessions, static-analysis auto-detect, W&B injection.

Layout: `launcher/` (Go), `core/` (Python).
