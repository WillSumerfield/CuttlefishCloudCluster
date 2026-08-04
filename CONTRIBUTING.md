# Contributing

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


- **One logical change per commit.** Split unrelated changes into separate commits, even within a
  single working tree. When a working tree is mixed, state the intent of each change in its message.
- **Rebase, never merge.** Keep history linear; rebase feature branches onto the base branch.

## Branching

- Work on a branch; never commit directly to `main`.
- No PRs — rebase the branch onto `main` and integrate directly.
