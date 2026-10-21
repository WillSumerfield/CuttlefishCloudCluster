# head/setup

Provisioning for the Head role, composed by `cuttle setup --head` (alias `--local`): self-hosted
Headscale coordinator + isolated `tailscaled-ccc` mesh, `slurmctld` + munge, the mesh-only enroll
API, the LAN+mesh web dashboard, rsync stage-out, the auth store + Karma, and a co-located Arm.

No `slurmdbd`/MariaDB — Karma sets Slurm priority itself and needs no accounting database.
