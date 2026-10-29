---
title: Arm setup
---
To lend your machine to CCC, you install `cuttle`, get a one-time token from an
[[Glossary#Admin|Admin]], and run one setup command. It takes a while (it installs packages and pulls
a few GB of images), but you only answer a couple of questions.

## What your machine needs

- **Ubuntu 24.04, x86_64.** Setup refuses anything else.
- **An NVIDIA GPU with its driver installed.** `nvidia-smi` must work and show your card. If the
  machine has several GPUs, they must all be the same model.
- **25 GB free** on the disk that holds `/var/lib/containers`.
- **sudo** on the machine.
- **A network path to the Head.** The Admin tells you the address.

You don't need to install Podman, Slurm or the NVIDIA container toolkit yourself. Setup does that.

### Windows machines (WSL)

A Windows PC can be an Arm through WSL2 running Ubuntu 24.04, with a few extra rules:

- **Exactly one GPU.** Setup refuses a WSL machine with more.
- **systemd on.** In the Ubuntu distro, `/etc/wsl.conf` needs:
  ```ini
  [boot]
  systemd=true
  ```
- **cgroup v2.** In Windows, add this to `%UserProfile%\.wslconfig`:
  ```ini
  [wsl2]
  kernelCommandLine = cgroup_no_v1=all
  ```
- After changing either file, run `wsl --shutdown` in Windows and reopen Ubuntu.
- **25 GB free on your Windows `C:` drive** as well. Setup checks both.

Setup checks all of this before it changes anything, and tells you which line is missing.

## Set it up

1. **Install `cuttle`.** Ask the Admin for the installer bundle (`cuttle-installer.tar.gz`). Unpack
   it and run the installer from inside the folder:
   ```bash
   tar xzf cuttle-installer.tar.gz
   cd <the unpacked folder>
   ./install.sh
   ```

2. **Get a token.** Ask the Admin to authorize your machine. They pick its name and run:
   ```bash
   cuttle arm authorize my-desktop --user you
   ```
   That prints an enrollment token, which they send you privately. Treat it like a password. It works
   for one machine, once, and runs out after an hour.

3. **Run setup on your machine:**
   ```bash
   cuttle setup --arm --token <the token>
   ```
   Or run `cuttle setup`, pick "An Arm", and paste the token when asked.

4. **Answer the questions.** Setup asks whether this machine has any of CCC's
   [[Arm features|Features]] (special software like `isaaclab`). Say no unless you know it does. At the
   end it offers to log you in, so you can start Dives from this machine too.

5. **Wait for it to finish.** Setup joins the cluster's private network, installs what it needs,
   pulls the images and runs a short benchmark on your GPU to measure its speed. When it's done it
   prints your Arm's name.

6. **WSL only: keep it running.** Windows stops WSL when you close the last Ubuntu window, which takes
   your Arm offline. Setup prints a `schtasks /create …` line at the end. Run it once in an
   administrator Command Prompt (cmd) and Windows keeps Ubuntu running in the background from logon.
   A sleeping or switched-off PC is offline like any other Arm.

## Check it worked

[Open Arms in the Dashboard](dashboard:/arms). Your machine shows up as a card under **My arms**. It should
read **idle** once it's ready, or **benchmarking** for a minute first. If it says **awaiting admit**,
ask the Admin to finish the job with `cuttle arm admit <name>`.

## Updates

You don't need to do anything when the Admin upgrades CCC: the Head updates your Arm for you. It
waits until your Arm is free, so a running Dive is never cut short, and your Arm takes no new Dives
until it's done. The [Arms page](dashboard:/arms) says **Updating after its current Dive** meanwhile.

If an update fails, the Arm stays out of service and the Arms page says **Update failed** (hover it
for the reason). The Admin gets an email too. Fix what it names, then ask them to retry.

You can always update by hand, on the Arm:

```bash
cuttle update
```

It asks no questions and keeps your settings. An Arm set up before automatic updates needs this once,
so the Head learns where its `cuttle` is installed.

If the Admin rotates the cluster key, they'll ask you to run:

```bash
cuttle arm sync-key
```

To change your answers (for example, to add a Feature), run setup again without a token:

```bash
cuttle setup --arm --enrolled
```

## Remove it

To take your machine out of CCC for good, run this on the Arm:

```bash
cuttle teardown --arm
```

It asks you to type the machine's hostname to confirm. It ends any Dives running on your machine,
leaves the cluster's network and removes what setup installed. Add `--keep-packages` to leave the apt
packages installed, or `--all` to also delete the downloaded images.

Then ask the Admin to run `cuttle arm remove <name>` on the Head. Your machine can't be enrolled again
until they do.

On WSL, teardown also prints a `schtasks /end … /delete …` line. Run it in an administrator Command
Prompt to remove the keep-alive task.

If you only want your machine back for a while, you don't need any of this. Retract it instead (see
[[Arms]]).

## Related
- [[Donating/index|Donating]]: parent
- [[Arms]]: what your Arm does once it's set up
- [[Arm features]]: the Feature questions setup asks
- [[Glossary#Arm|Arm]]: the term
