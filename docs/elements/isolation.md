# Isolation

## What it is

Your work runs on somebody else's computer. Somebody else's work runs on yours.

This page is what stops those two facts from being a problem. There are three moving parts, and the
useful thing about them is that they are enforced by the **operating system** rather than agreed to
by convention — CCC is not asking anyone's job to behave.

- **Pod** — the walls. *Where* your work runs.
- **Guise** — the identity. *Who* it runs as.
- **Siphon** — the channel. *What it can reach* back on the Head.

There is nothing to configure here and no commands. It is how every Spawn already works.

For the trust model as a whole — who is trusted with what, and what CCC deliberately does not
defend against — see [Security](../Security.md).

## Behavior

### Pods — where it runs

A **Pod** is the isolated place a job runs inside, on whichever machine is running it.

The walls are real, and they enclose the **whole** Spawn — your code *and* the wrapper that stages
your files in and out, not just your code. From inside one, a Spawn cannot see:

- the host's filesystem,
- the machine's other processes,
- any concurrent Spawn's working files, including their scratch space.

A Pod is the *walls*, never the contents. What is installed inside one is that Spawn's
[Environment](environments.md).

Your Spawn enters a Pod twice: once to build its [Environment](environments.md), and once to run your
script. Same walls, same Guise, same machine, one step apart. A dependency file can run arbitrary build
hooks, so it is code, and CCC runs no User's code unwalled anywhere.

### Guises — who it runs as

A **Guise** is the identity a Spawn runs as on the machine running it. One per User, the same one
wherever the cluster places your work, adopted by a machine the first time your work lands there.

It is what stops one User's Spawn from reading, killing or interfering with another's.

A Guise is **not an account anybody can log into**. It authenticates nobody, carries no authority,
and has nothing to do with your [session](users-and-access.md). It is not you-on-that-machine; it is
a boundary with your name on it.

It also is not the cluster. Anything CCC does on your behalf that involves *your code* — including
building your Environment — is done as your Guise, never as the cluster's own identity. Your code has
no route to becoming the cluster, on any machine, including the Head.

### Siphons — what it can reach

A **Siphon** is the one channel through which a Spawn draws its project in and sends its results home.
It opens when the Spawn is submitted and closes once its transfers have finished.

It reaches exactly that Spawn's own uploaded work and its own results, and **nothing else the Head
holds**. So work running on a donated machine cannot read another User's project or write into
another Spawn's results — even though every Arm shares one [Mesh](mesh.md).

The distinction that matters: being on the Mesh admits a *machine*. A Siphon authorizes one *Spawn*.

A Siphon's closing is tied to that Spawn's last transfer, not to its billing. A Spawn's final copy
home can still be arriving well after it has been settled.

### The Mantle — who the cluster is

The cluster has its own identity, called the **Mantle**. It owns what the cluster owns: the account
records, the Head's key, every User's uploaded work, every Spawn's results.

It is nobody's. No User is ever the Mantle, and nothing a User submits ever runs as it.
