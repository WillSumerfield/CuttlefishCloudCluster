# Mesh

## What it is

The **Mesh** is the cluster's own private encrypted network. Every Head and every Arm is a member,
and they address each other only by their mesh address.

Each cluster has exactly one Mesh, and two clusters' Meshes never see each other.

The reason it exists is that donating a machine should not mean giving anyone access to your
network, or you access to theirs. The Mesh is a network that contains the cluster and nothing else.

Being on the Mesh authorizes a **machine**. It says nothing about people — that is your
[session](users-and-access.md) — and it is not what lets one Spawn reach another's files, which it
cannot ([Siphons](isolation.md#siphons--what-it-can-reach)).

## Behavior

### It does not touch your existing network

CCC runs its own isolated networking daemon, separate from anything else on the box. It deliberately
stays off two host-global things a second daemon would otherwise fight over: it does not take over
DNS, and it does not touch your route table.

Firewall rules are the exception, and there are exactly two. CCC opens its own mesh interface (`ccc0`)
if — and only if — you already run `ufw`; it never switches a firewall on for you. And if you use
Tailscale personally, it adds one rule to keep that Tailscale from dropping CCC's traffic.

### If you already use Tailscale personally

CCC's mesh runs alongside your own Tailscale, on its own daemon, and neither is aware of the other.
One thing does have to be arranged between them.

Tailscale reserves a large block of addresses (`100.64.0.0/10`) and, by default, drops anything from
that block that did not arrive on its own network interface — a sensible anti-spoofing measure. CCC's
mesh addresses have to live inside that same block, because Tailscale refuses to route anything
outside it, and they arrive on `ccc0`. Left alone, your personal Tailscale therefore drops every CCC
packet, and this machine goes quiet the moment both are running.

So CCC adds one narrow rule exempting `ccc0`, and a small timer that puts the rule back — your
Tailscale rebuilds its firewall from scratch every time it restarts, including on every reboot, which
is what takes the rule away. The exemption grants nothing: it only says "packets on `ccc0` are not
spoofed", and your firewall still decides what they may reach. `cuttle teardown` removes it.

If you administer your own tailnet and would rather fix it upstream, the node attribute
`disable-linux-cgnat-drop-rule` in your tailnet's policy file tells Tailscale not to install that DROP
at all. CCC cannot do this for you — it is a setting on *your* tailnet, not on the cluster.

### Joining is by token, and only by token

The **Coordinator** — a service on the Head — is what admits machines and gives each a stable mesh
address. To join, a machine needs an **enrollment token**, minted by the Admin with
[`cuttle arm authorize`](arms.md#admin--on-the-head).

The token is one-time, one-machine, and expires within the hour. It also names the Arm's owner,
chosen by the Admin when they mint it — so the donor redeems it and declares nothing, and the Head
accepts them without a second approval step.

It is the only thing that admits a new Arm. The Admin's own password never leaves the Head.

> A token is a **secret**. Anyone holding it can put a machine on your cluster's network. Send it
> privately.

### The coordinator's address is the one hop before the Mesh

A machine contacts the Coordinator in order to *get* a mesh address. So the Coordinator's own address
can never be a mesh address — it is the one hop that happens before the Mesh exists.

By default the Head advertises its LAN IP, which only machines on that same LAN can reach. Setting up
a remote Arm against a LAN-only address hangs until it times out. See
[Set up the Head](../setup/head.md#making-the-head-reachable) for the fix.

### Registration

Once on the Mesh, an Arm registers itself: its node agent announces its own hardware to the
scheduler, at runtime, rather than the Head being told in advance what to expect. This is why
`cuttle arm list` distinguishes *enrolled* (on the Mesh, hardware reported) from *admitted* (Slurm
will place work on it).

### The munge key

The nodes of the cluster authenticate to each other with a shared cluster-wide key. Mesh membership
is what authorizes fetching it — which is exactly why it is safe to hand out over the Mesh and
nowhere else.

You will only ever meet it in one situation: the operator rotates it, and your Arm needs the new one.

```sh
cuttle arm sync-key      # run this ON the Arm
```

That fetches the current key and changes nothing else — mesh identity, GPU declaration and scheduler
config are all left alone.

## Admin — on the Head

**`cuttle munge rotate`** mints a fresh cluster-wide key. Every already-admitted Arm's copy goes
stale the instant it returns, and each one needs its owner to run `cuttle arm sync-key` before it can
reach the scheduler again. The command prints the list of Arms that need it, so you know who to tell.

**`cuttle arm remove <name>`** takes an Arm off the Mesh as well as out of Slurm and the registry.
All three, because a departed donor left on the Mesh keeps the membership that authorizes fetching
the munge key.

One thing removal does *not* do: it does not invalidate that box's existing copy of the key — munge
has no per-holder revocation. If that matters, rotate.
