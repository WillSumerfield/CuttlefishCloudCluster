# arm/setup

Provisioning for the Arm role, composed by `cuttle setup --arm --token <blob>`: Linux-layer bring-up,
unattended mesh join against an admin-minted enrollment token, munge-key
fetch, and a dynamic `slurmd -Z` that fetches its config from the Head (configless). Apptainer +
`base.sif` for Pods.

Every Arm is the same kind of Arm; its owner reclaims it with **Retract**, which needs nothing
declared at setup.
