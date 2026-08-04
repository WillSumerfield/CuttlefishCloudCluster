# Testing policy

> What earns a test in CCC, and what does not. Terms are defined in [`CONTEXT.md`](CONTEXT.md);
> how to run the suite is in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

CCC is a research cluster built by one person. The suite exists to catch what a person cannot see:
values that are accepted happily where they are written and only fail later, somewhere else, on a
machine nobody is watching. It does not exist to describe the code back to itself.

## The bar

**A test must name a consequence that outlives the moment.** Write it in the docstring. If the
consequence cannot be stated in one sentence, there is no test to write.

Real consequences look like:

- a job that hangs forever waiting for a keystroke nobody can send (CI, cron, a pipe)
- a Spawn priced wrongly, a balance that does not sum to zero, an economy that silently stops gating
- a `slurm.conf`, unit file, or job script that renders wrong and only fails on a donor's box
- data destroyed without confirmation — a user, a ledger, a cluster
- stdout corrupted, so `id=$(cuttle run …)` breaks
- a secret that exists exactly once being lost (the one-time admin password)
- the terminal left in a state the user has to `reset` out of

Not consequences: how something looks, what colour it is, that a constant still holds the value it
was assigned one line earlier, that a library was called with the argument it was just passed.

## The UI is verified by running it

The interactive layer — boxes, borders, cursor arithmetic, menu rendering, spinners — is checked by
typing `cuttle` and looking at the screen. It is the one part of CCC whose failures are *immediately
visible to the only person who runs it*, which is exactly the property the rest of the codebase
lacks and the reason the rest is tested.

The tests that remain around the UI are there for what happens **off** the screen: what reaches
stdout, whether a script can block, whether a secret survives, whether the terminal is handed back.

This is not an exception to the bar — it is the bar. A UI test that cannot name a consequence is a
screenshot with extra steps.

## Bugs found by eye do not become tests

When a UI bug is found by looking at it, fix it and look again. Do not add a regression test.

The temptation is strongest here and should be refused: "it broke once" is how a suite grows back
after it has been cut. A bug that is visible the moment you run the command does not need a test to
stay visible. A bug that is *not* visible when you run the command was never a UI bug — it had a
consequence, and the consequence is what gets the test.

## One wiring test, not a pattern

Prompts are tested through a seam that replaces the prompt layer wholesale. A seam cannot see that
layer being broken — a terminal mode that mangles every newline, a key that never decodes, a frame
that never paints — so exactly one test drives a real pty end to end
(`test_the_menu_actually_renders_on_a_real_terminal`, marked `slow`).

One. It is a smoke check that the wiring exists, not a technique to reach for again. A second pty
test is a sign the thing being tested belongs behind the seam.

## Mutation check

A test earns its place by failing. Break the code it guards and confirm *that* test is the one that
goes red. A test that still passes against the broken code is not a test, and the discovery is
usually that the assertion was measuring something adjacent to the claim.

Two examples from this repo, both of which passed while the code was broken:

- a scripted-human seam that accepted a `None` answer the real prompt rejected — so the menu's
  "← back" shipped broken with the full suite green
- a stdout assertion that checked only final contents, which held whether output was buffered or
  written straight through — the actual claim was about *timing*

## Scale

If a file's test count approaches that of `test_karma.py` (the economy, ~70), that is a signal to
re-read this document, not a sign of thoroughness. The interactive layer is not as risky as the
economy, and its test count should say so.
