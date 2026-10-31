# gateway/setup

No code of its own. Provisioning for the network fabric — WireGuard mesh (Tailscale/Headscale)
enrolment and built-in Head auth (sign-on) wiring — lives in `cli/core/src/cuttle/mesh.py` and
`cli/core/src/cuttle/auth.py` (+ `head/api/src/ccc_head_api/atrest.py` on the Head side). Composed by
`cuttle setup` on every role (all nodes join the mesh).
