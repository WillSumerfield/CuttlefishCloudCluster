# tests — Glossary

> Local glossary for CCC's test suite. Root terms in [`../CONTEXT.md`](../CONTEXT.md).
> Glossary only — no implementation details. Conventions live in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

CCC is a distributed system whose every interesting action needs something a test cannot have: Slurm,
a GPU, root, a mesh, a donor's machine. The suite exists to test the parts that decide what those
things are *told* — and, at the end of a phase, to test the whole economy against a **Fake Cluster**
that behaves like Slurm without being it.

- **Seam** — a contract two contexts agree on without ever stating it in one place: the CLI renders an
  Arm's Slurm feature, and the Head API matches a Spawn's declared need against that feature. Neither
  side can be wrong alone; they can only *disagree*. Seams are where CCC's silent bugs live, so the
  suite is deliberately arranged to see both sides of one at once.
- **Remote consequence** — a wrong value that is accepted happily where it is written and only fails
  somewhere else, later, on a machine nobody is watching: a mangled `slurmd` flag, a job script that
  renders invalid Python, a priority constant that silently disables the whole economy. The defining
  property of the code this suite exists to guard.
- **Fake Cluster** — an in-memory stand-in for Slurm: it accepts Spawns, ranks them the way
  `priority/multifactor` ranks them, starts and ends them as simulated time advances, and refuses the
  things real Slurm refuses (a job owner may not set a negative nice). Not a mock of the calls we make
  — a model of the behaviour we depend on.
- **Pipeline test** — drives the *real* economy code against the **Fake Cluster** and a real (temporary)
  ledger, across simulated weeks. It is the only way to test the claims that exist solely over time:
  that Karma **never gates**, that balances sum to zero, that flooding the queue costs the flooder.
- **Hardware verification** — the manual gate a Fake Cluster can never replace: real Slurm, real sudo,
  real mesh, a real donor's GPU. Recorded per slice as a short list of things to run and see. Everything
  the fake mocks away is exactly what this catches.
- **Mutation check** — the way a test earns its place: break the code it guards, and confirm *that*
  test is the one that fails. A test that still passes against the broken code is not a test.
