# head/setup

Provisioning for the Head role, composed by `cuttle setup --head` (alias `--local`): self-hosted
Headscale coordinator + isolated `tailscaled-ccc` mesh, `slurmctld` + munge, the mesh-only enroll
API, the LAN+mesh web dashboard, rsync stage-in/out, and a co-located Arm.

`slurmdbd` + MariaDB (job accounting + FairShare, backing the Karma sync) is planned for the economy
phase — not yet provisioned.
