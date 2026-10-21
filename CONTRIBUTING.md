# Contributing

## Repository layout

Monorepo, split by **deployment context**. Each top-level dir is one bounded context with its own
`CONTEXT.md`; deployable contexts also carry a `setup/` for provisioning.

| Context | Role |
|---|---|
| [`cli/`](cli/) | The `cuttle` tool — a single Python CLI (setup/teardown, submit, follow, admin). |
| [`head/`](head/) | Control plane: Headscale coordinator, `slurmctld`, enroll API + web dashboard, stage-in/out, auth + Karma (SQLite `head.db`). |
| [`arm/`](arm/) | Compute node: Linux-layer bring-up, `slurmd`, Apptainer, staging. |
| [`karma/`](karma/) | Karma economy: wallets, pricing (the Tide), congestion, override. Ships inside `head/api` — see `karma/setup/README.md`. |
| [`telemetry/`](telemetry/) | W&B injection, Head-box rogue-actor detection + email alerting. |
| [`gateway/`](gateway/) | Network fabric (WireGuard mesh) + built-in Head auth (sign-on). Lives in `cli/core` + `head/api` — see `gateway/setup/README.md`. |
| [`shared/`](shared/) | The CLI↔Head contract as a concept (no dedicated package yet — see `shared/CONTEXT.md`). |
| [`docs/`](docs/README.md) | User-facing documentation. |
| [`examples/`](examples/) | Worked projects, one per Environment path, plus a Feature example. |

Architecture — how these fit together at runtime, and why — is
[`docs/Architecture.md`](docs/Architecture.md). Terminology is [`CONTEXT.md`](CONTEXT.md).

## Commit convention

Format: **Conventional Commits**, with the **scope restricted to a top-level context**.

```
type(scope): subject
```

- **type** — one of: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `chore`, `build`, `ci`.
- **scope** — exactly one top-level context: `cli`, `head`, `arm`, `karma`, `telemetry`,
  `gateway`, `shared`, `docs`. Use `repo` for cross-cutting/tooling changes with no single context.
- **subject** — concise, imperative, lowercase, no trailing period. Sacrifice grammar for concision.

Examples:

```
feat(karma): add tide pricing curve
fix(arm): grace-exit running job on reclaim
docs(telemetry): note rogue-actor 10% threshold
chore(cli): scaffold setup dispatch
```

## Tests

`uv run pytest` from the repo root. The root `pyproject.toml` is a **virtual** uv project (nothing to
build): it exists only to pull `cuttle` and `ccc-head-api` into one venv, because the behaviour worth
testing tends to live on the *seam* between them — the CLI renders a node's Slurm feature, the Head API
matches a Spawn's request against it, and a test that can see only one side cannot check that they
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
