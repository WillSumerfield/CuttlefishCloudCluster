---
title: Roadmap
---
CCC is partway through its 2.0 rebuild: every User gets a [[Glossary#Tidepool|Tidepool]] on the
[[Glossary#Head|Head]], and all GPU work is a [[Glossary#Dive|Dive]]. This page shows where that
stands. It changes often; the working plan is `temp/2.0-plan.md` in the repo.

## Where things stand

| Phase | What | Status |
| --- | --- | --- |
| 0 | Investigations: container runtime (Podman), campus wifi access | Done |
| 1 | Removals: 1.x batch jobs and everything around them | Done |
| 2 | Podman in place of Apptainer, registry, `ccc-launch`, WSL Arms | Done, hardware-tested |
| 3 | Tidepools on the Head | Built, hardware-tested |
| 4 | Dives | Built, hardware-tested |
| 4b | One Dive path: Head Dives run exactly like Arm Dives | Built, tested on the Head |
| 4c | apt installs made in a Dive come back to the Tidepool | Built, tested on real boxes |
| 5 | Transfer: `cuttle upload` / `download`, Dashboard Files page | Built, mostly hardware-tested |
| 6 | Campus access: login throttling | Built; one end-to-end check left |
| 7 | Docs move to this wiki | Done |
| 8 | Loose ends: Feature runtime profiles in Dives | Built; not yet hardware-tested |

## Next up

- **Phase 8 on real hardware.** The `vlearn` profile is wired end to end — the Feature's package and
  system libraries reach a Dive, the licensed package is swapped for the Arm's own, the machine is
  activated at `setup --arm`, and the Licence directory is bound into the Dive. None of it has run on a
  real box with a real licence and GPU yet.

## Still to test on real hardware

Unit tests can't cover these. They need real boxes.

- A campus-wifi laptop, end to end: `cuttle setup --client` with the Head's public hostname, login,
  Dashboard, `cuttle tidepool`, attaching to an Arm Dive through the relay, upload and download.
- The Dashboard Files page in a browser, especially drag-and-drop upload. The API behind it passes.
- Replaying a User's package list after a Tidepool rebuild (unit-tested only).
- A live `vlearn` Dive on an Arm with a real licence and GPU: activation at `setup --arm`, the
  Feature's wheel + system libraries + Licence directory reaching the Dive, and `create_gym()`.

## Known gaps

- A snapshot pushed for a Dive whose `dive-open` then failed stays in the registry until someone
  cleans it up by hand.
- Nothing caps how long a low-Karma User can hold GPUs. A Karma-based maximum duration is an idea, not
  a plan yet.

## Related
- [[Contributing/index|Contributing]]: parent
- [[Architecture]]: how the pieces above fit together
- [[Isolation]]: the container and network fences these phases built
