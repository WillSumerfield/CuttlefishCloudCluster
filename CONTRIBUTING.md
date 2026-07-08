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

## Commit hygiene

- **One logical change per commit.** Split unrelated changes into separate commits, even within a
  single working tree. When a working tree is mixed, state the intent of each change in its message.
- **Rebase, never merge.** Keep history linear; rebase feature branches onto the base branch.

## Branching

- Work on a branch; never commit directly to `main`.
- Open a PR on GitHub; rebase (not merge) to integrate.
