# arm/setup

Provisioning for the Arm role, composed by `cuttle setup --arm --invite <blob>`: Linux-layer bring-up
(native / WSL2 today; macOS VM planned), operator-approved mesh join, munge-key fetch, and a dynamic
`slurmd -Z` that fetches its config from the Head (configless). Apptainer + `base.sif` for Pods.

The `--volatile` flag and Retract/preemption hooks are planned for the hardening phase — not yet built.
