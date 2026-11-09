# Contributing

## Repository layout

Monorepo, split by **deployment context**. Each top-level dir is one bounded context with its own
`CONTEXT.md`; deployable contexts also carry a `setup/` for provisioning.

| Context | Role |
|---|---|
| [`cli/`](cli/) | The `cuttle` tool — a single Python CLI (setup/teardown, Dives, follow, admin) plus the root helpers (`ccc-tidepool`, `ccc-launch`). Arm-side code lives here too. |
| [`head/`](head/) | Control plane: Headscale coordinator, `slurmctld`, Head API + Enroll API, Karma tick, relay, and the web Dashboard (`head/web/`). Auth + Karma in SQLite `head.db`. |
| [`arm/`](arm/) | Compute node: Linux-layer bring-up, `slurmd`, Podman, staging. |
| [`karma/`](karma/) | Karma economy: wallets, pricing (the Tide), congestion, override. Ships inside `head/api` — see `karma/setup/README.md`. |
| [`telemetry/`](telemetry/) | Head-box rogue-actor detection + email alerting. |
| [`shared/`](shared/) | The CLI↔Head contract as a concept (no dedicated package yet — see `shared/CONTEXT.md`). |
| [`wiki/`](wiki/) | The project's documentation — a Quartz site, pages in `wiki/content/`, served by the Head at `/wiki/`. |
| [`examples/`](examples/) | Worked projects, one per Environment path, plus a Feature example. |

Architecture — how these fit together at runtime, and why — is the wiki's
[`Contributing/Architecture`](wiki/content/Contributing/Architecture.md). Terminology is
[`CONTEXT.md`](CONTEXT.md). The `## Wiki` section of [`CLAUDE.md`](CLAUDE.md) has the page rules.

## Commit convention

Format: **Conventional Commits**, with the **scope restricted to a top-level context**.

```
type(scope): subject
```

- **type** — one of: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `chore`, `build`, `ci`.
- **scope** — exactly one top-level context: `cli`, `head`, `arm`, `karma`, `telemetry`, `shared`,
  `wiki`. Use `repo` for cross-cutting/tooling changes with no single context.
- **subject** — concise, imperative, lowercase, no trailing period. Sacrifice grammar for concision.
- Wiki changes go in **their own commits**, separate from code.

Examples:

```
feat(karma): add tide pricing curve
fix(arm): grace-exit running dive on reclaim
docs(telemetry): note rogue-actor 10% threshold
wiki(using): dive detach/wait for scripts
```

## Tests

`uv run pytest` from the repo root. The root `pyproject.toml` is a **virtual** uv project (nothing to
build): it exists only to pull `cuttle` and `ccc-head-api` into one venv, because the behaviour worth
testing tends to live on the *seam* between them — the CLI renders a node's Slurm feature, the Head API
matches a Dive's request against it, and a test that can see only one side cannot check that they
agree.

What belongs here: **pure functions with remote consequences.** Everything under `tests/` renders a
config, a unit file, a job script, or an HTTP response — none of it needs Slurm, sudo, a GPU, or a
database. That is the point. A wrong `slurm.conf` or `--conf` string is silent when it is written and
only surfaces later, on a donor's box, as a node that registered and then can't be scheduled.

Assert on **parsed structure, not substrings**. `"PriorityType=priority/multifactor" in conf` passes
happily against a line that has been commented out — which is exactly how that setting would get lost.

What the suite does **not** cover — provisioning, real placement, isolation, and the interactive
layer — is [`tests/MANUAL.md`](tests/MANUAL.md), a checklist walked by hand before a release. The
policy for what earns an automated test is [`tests/TESTING.md`](tests/TESTING.md).

- **One logical change per commit.** Split unrelated changes into separate commits, even within a
  single working tree. When a working tree is mixed, state the intent of each change in its message.
- **Rebase, never merge.** Keep history linear; rebase feature branches onto the base branch.

## Branching

- Work on a branch; never commit directly to `main`.
- No PRs — rebase the branch onto `main` and integrate directly.
