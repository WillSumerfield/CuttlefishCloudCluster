---
title: Mesh
---
The [[Glossary#Mesh|Mesh]] is CCC's private network between the Head and every
[[Glossary#Arm|Arm]]. An Arm can be on any network, as long as it can reach the Head once to join;
after that, the scheduler, file copies and container images all travel over the Mesh. Users' laptops
are never on it: they talk to the Head directly.

## How it works

The Head runs its own coordinator, **Headscale**, and every machine in the cluster (the Head
included) runs a Tailscale client that joins it. The traffic between them is encrypted WireGuard.

On the Head you'll see:

- `headscale`: the coordinator, listening on port **8080** over HTTPS, with its own certificate.
- `tailscaled-ccc`: CCC's own Tailscale client. Its network interface is `ccc0`, its socket is
  `/run/tailscale-ccc/tailscaled.sock`, and it's separate from any Tailscale already on the box.
- `ccc-mesh-firewall.timer`: see [Next to a personal Tailscale](#next-to-a-personal-tailscale).

A few things to know:

- Mesh addresses come from `100.99.0.0/16`. Machines reach each other by IP; there's no Mesh DNS, and
  CCC doesn't touch the box's own DNS or routes.
- Every machine on the Mesh can reach every other one on any port. Being on the Mesh means being
  trusted as part of the cluster, so the only gate is who you let join.
- The Enroll API (port 8081) and the file-copy service (port 873) only listen on the Head's Mesh
  address.

## The coordinator address

An Arm has to reach the coordinator **before** it's on the Mesh, to get a Mesh address in the first
place. So the coordinator's address is an ordinary address the donor's machine can already reach:

- every donor on your LAN: the Head's LAN IP is fine;
- some donors elsewhere: use the Head's public IP or hostname.

You set it with `--address` at [[Head setup]]. If you pick a private LAN address, setup warns you
that remote Arms won't be able to join (they hang at `cuttle setup --arm` until it times out).
`cuttle arm authorize` warns again before it mints a token.

The address is remembered: a plain `cuttle setup --head` keeps it. To change it, re-run with the new
one:

```bash
cuttle setup --head --address=<new address>
```

Setup asks you to confirm, because every token already handed out stops working and every Arm
re-registers against the new address.

## Adding an Arm

Arms join by **enrollment token**, and only by token. You decide which machine joins and who owns it;
the donor runs one command.

1. Make sure the owner has an account (`cuttle user add`, see [[Users and access]]).
2. On the Head:

   ```bash
   cuttle arm authorize lab-pc-2 --user alice
   ```

   Arm names are lowercase letters, digits and `-`. This prints a token.
3. Send the token to the donor **privately**. It lets one machine onto your network, once, within the
   hour.
4. The donor runs, on their machine:

   ```bash
   cuttle setup --arm --token <token>
   ```

   See [[Arm setup]] for their side.
5. `authorize` waits up to five minutes for them, then admits the Arm to the scheduler. If the donor
   wasn't ready in time, finish the job once they've run setup (the token stays good for its hour):

   ```bash
   cuttle arm admit lab-pc-2
   ```

The Arm benchmarks its own GPUs once it's admitted, which sets how much Karma it earns. Re-run that
after a GPU or driver change with `cuttle arm benchmark <name>`.

`cuttle arm list` shows every Arm and where it is:

| State | Means |
| --- | --- |
| authorized, awaiting the donor | Token made, machine hasn't joined yet |
| enrolled, NOT ADMITTED | On the Mesh, but not taking Dives. Run `cuttle arm admit <name>` |
| admitted | Taking Dives |
| head | The Head's own Arm |

## Removing an Arm

```bash
cuttle arm remove lab-pc-2
```

You type the name to confirm. It takes the Arm out of the scheduler and the registry, removes it from
the Mesh, and revokes its access to the image registry. Coming back means a new token.

To give an Arm a new owner without touching the machine, use `cuttle arm chown <name> --user <owner>`.

## The munge key

Every machine in the cluster proves itself to the scheduler with one shared key, the munge key. An
Arm fetches it over the Mesh when it joins. Removing an Arm doesn't make that machine's copy stop
working, so if you don't trust a removed machine, rotate the key:

```bash
cuttle munge rotate
```

It prints every Arm that now has a stale key. Each owner has to run `cuttle arm sync-key` on their
machine before it can take Dives again.

## Next to a personal Tailscale

If the Head or an Arm also runs a personal Tailscale, that Tailscale drops Mesh traffic by default.
CCC adds one firewall rule that lets `ccc0` traffic through, and `ccc-mesh-firewall.timer` puts it
back every minute, since Tailscale wipes it whenever it restarts. `cuttle teardown` removes it.

## When the Mesh is down

```bash
systemctl status headscale tailscaled-ccc ccc-mesh-firewall.timer
sudo headscale nodes list                                       # who's joined, and online?
tailscale --socket=/run/tailscale-ccc/tailscaled.sock status    # the Head's view of the Mesh
journalctl -u headscale -u tailscaled-ccc -n 100
```

Use the `--socket` form. A plain `tailscale status` talks to the box's personal Tailscale, if it has
one, not to CCC's.

Common causes:

- **A new Arm hangs at setup.** It can't reach the coordinator address. Check it from the donor's
  machine. A Head set up before setup opened port 8080 needs `cuttle update` (or
  `sudo ufw allow 8080/tcp`) if `ufw` is on.
- **"authkey already used" or an expired token.** Each token works once, for an hour. Run
  `cuttle arm authorize` again for a new one.
- **An Arm is enrolled but never admitted.** `cuttle arm admit <name>`.
- **Arms dropped off after a key rotation.** Their owners need `cuttle arm sync-key`.
- **Everything stopped after a reboot on a box with personal Tailscale.** Check
  `ccc-mesh-firewall.timer` is running.

## Related

- [[Admin/index|Admin]]: parent
- [[Head setup]]: where the coordinator address is chosen
- [[Arm setup]]: the donor's side of joining
- [[Arms]]: what an Arm is, for donors
- [[Security]]: what being on the Mesh lets a machine do
- [[Admin troubleshooting]]: other problems on the Head
