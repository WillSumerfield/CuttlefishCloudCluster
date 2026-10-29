---
title: Arm features
tags: [leaf]
---
A Feature is a piece of special software your Arm has that most machines don't, like a big simulator
install or software licensed to your machine. You tell CCC about it during setup, and Dives that need
it are sent only to Arms that have it.

## The Features CCC knows

The list is built into CCC. You can't add your own names. To see it:

```bash
cuttle feature list
```

Today there are two:

- **`isaaclab`**: a large install that isn't on every machine.
- **`vlearn`**: software licensed to one particular machine, so a Dive has to use your Arm's own copy
  and licence.

To see what CCC sets up for a Feature (system libraries, which Python it needs):

```bash
cuttle feature show vlearn
```

## Declaring a Feature

`cuttle setup --arm` asks about each Feature in turn:

1. **Does this Arm have it?** Say yes only if the software is really installed here. A Dive that asks
   for a Feature can only run on an Arm that has it, so a wrong yes sends someone's Dive to a machine
   that can't run it.
2. **Where is it installed?** Give the full path, such as `/opt/isaaclab`. For `vlearn`, setup checks
   the folder really holds a `vlearn` install and asks again if it doesn't.
3. **Where is the product key?** `vlearn` only. Point it at the `License.key` the software was
   activated with. CCC keeps a copy on your machine, readable only by CCC's own accounts. It never
   leaves the machine.

### The licence directory

If you declare `vlearn` (or say this machine holds a machine licence), setup also asks for a **licence
directory**: a folder that doesn't exist yet, such as `/opt/ccc/licence`. CCC creates it as the home of
its own `ccc-licence` account. Licensed software looks for its activation files there.

Don't use your own home folder. Anything in the licence directory can end up inside Dives that use
a Feature on your Arm.

## Changing your Features

Run setup again without a token. It shows what you declared last time and asks whether to keep it:

```bash
cuttle setup --arm --enrolled
```

Answer no to walk through the list again. `cuttle update` never changes your Features.

## Using a Feature in a Dive

People ask for a Feature when they start a Dive:

```bash
cuttle dive start my-project --time 4h --feature isaaclab
```

If no Arm in the cluster has it, the Dive is refused straight away with a list of the Features the
cluster does have. See [[Dives]].

## Related
- [[Donating/index|Donating]]: parent
- [[Arm setup]]: where setup asks these questions
- [[Arms]]: what an Arm does day to day
- [[Dives]]: asking for a Feature
