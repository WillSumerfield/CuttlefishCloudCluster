"""The zsh a **Tide Pool** hands its User: `/etc/zsh/zshrc`, rendered here and written into the Pod by
`spawns.PAYLOAD_TIDEPOOL`'s boot script at every session.

**Why the Head renders this instead of `base.sif` baking it.** The image is built once per node and
never compared against its definition afterwards (`cuttle.setup.build_base_sif`), so anything inside it
can only be changed by a fleet-wide `teardown --all`. That is the right home for the zsh binary and the
oh-my-zsh tree, which change approximately never. It is the wrong home for a prompt, which is the one
part anybody actually iterates on — baking it would price every cosmetic tweak at a teardown of every
enrolled box plus a client re-pin. `cuttle.setup.install_env_tools` makes the same argument for keeping
uv and micromamba out of the image; this goes one step further, because the Head is already rendering
the rest of this Pod's boot and has somewhere to put it for free.

So: the *tree* is in the image at OMZ_DIR (cuttle.templates.BASE_DEF_TMPL puts it there), and the two
files that configure it come from here. Changing a prompt is an edit plus a Head API restart, and the
next Tide Pool has it.

**Everything zsh writes is redirected to /ccc-tmp, and that is not tidiness.** `--contain` gives the Pod
an empty tmpfs at the Guise's home, carved out of the same 64 MB that backs `--writable-tmpfs` (see
spawns.py's note above PAYLOAD_TIDEPOOL). Two things follow. The small one is the budget: a compdump and
a history file are nothing, but they share that 64 MB with everything else the session writes. The large
one is that for a Spawn requesting an Arm **Feature**, the home is not private at all — `licbind`
(spawns.py) binds the Arm's **Licence** directory over it, a `2770 ccc-licence:ccc-guises` tree shared by
every feature Pod on that Arm, and every **Guise** is in that group. Shell history written there would be
one User's keystrokes in a directory another User's Tide Pool can read. `/ccc-tmp` is this Spawn's own
node-local scratch, destroyed by the job script's EXIT trap, and is the only correct answer for all of it.

This is also why ZSH_CACHE_DIR has to be created *before* oh-my-zsh.sh is sourced rather than left to
omz: omz tests `[[ ! -w "$ZSH_CACHE_DIR" ]]` and silently falls back to `$HOME/.cache` when the
directory is not there yet, and only mkdirs it several lines later. A dir that does not exist is not
writable, so setting the variable without creating it lands the cache in exactly the shared home this
module exists to avoid.
"""

from pathlib import Path

# Where cuttle.templates.BASE_DEF_TMPL puts the vendored tree inside the image (the gitstatusd binary
# rides along inside it — see the note on GITSTATUS_CACHE_DIR below). Duplicated rather than imported —
# `cuttle` and `ccc_head_api` are two distributions that do not depend on each other, the same split
# environments.TOOLS_DIR already carries. If this moves, it moves in both files.
OMZ_DIR = "/opt/ccc/oh-my-zsh"

# Where the boot script writes what this module renders. Under /etc/zsh because that is where zsh looks
# with no wiring at all: `/etc/zsh/zshrc` is read for every interactive shell, needs no ZDOTDIR, and
# does not depend on $HOME existing or being the Guise's own. A User's own ~/.zshrc is still read after
# it and still wins, which a ZDOTDIR would have taken away.
POD_ZSHRC = "/etc/zsh/zshrc"
POD_P10K = "/etc/zsh/ccc-p10k.zsh"

# Everything zsh, oh-my-zsh, gitstatus and zoxide want to write, in one place on bound disk.
POD_ZSH_STATE = "/ccc-tmp/zsh"

# **ZDOTDIR, and why a Pod cannot do without one.** zsh runs `zsh-newuser-install` — a full-screen
# "you have no zsh startup files" wizard — whenever NONE of `.zshenv`, `.zprofile`, `.zshrc`, `.zlogin`
# exists in `${ZDOTDIR:-$HOME}`. The file above is a *global* rc, and a global rc does not count: it is
# read, and the wizard still fires. `--contain` gives every Pod an empty home, so without this every
# Tide Pool opened on the wizard instead of a prompt. (Measured: setting ZDOTDIR alone does NOT
# suppress it — only an actual file in the directory it names does, which is what the stub below is.)
#
# It points at this Spawn's own scratch rather than at the home, and that is the security half. zsh
# sources `$HOME/.zshrc` after the global rc, and on a Spawn requesting an Arm **Feature** the home is
# the Arm's **Licence** directory (`licbind` in spawns.JOB_SCRIPT) — `2770 ccc-licence:ccc-guises`,
# shared by every feature Pod on the node, with every **Guise** in the group. A `.zshrc` left there by
# one User would be sourced by the next User's shell. That is the hole StrictModes already closes for
# `authorized_keys` (see the note above PAYLOAD_TIDEPOOL), reopened by the shell instead of sshd; the
# obvious fix for the wizard — touching `$HOME/.zshrc` — would have seeded it. Pointing ZDOTDIR at
# private, per-session scratch closes it and fixes the wizard with one file.
POD_ZDOTDIR = POD_ZSH_STATE
POD_USER_ZSHRC = f"{POD_ZSH_STATE}/.zshrc"

# The User's own rc, which is theirs to edit and is read AFTER the rendered one below, so it still wins.
# Its real job is to exist: see POD_ZDOTDIR.
USER_ZSHRC_STUB = """\
# Yours, for this session. Read after CCC's own rc (/etc/zsh/zshrc), so anything here wins.
# This directory is your Spawn's private scratch and dies with the session — nothing here is kept.
"""

# The powerlevel10k configuration, verbatim as `p10k configure` wrote it. Kept as a data file rather
# than a string in this module so it stays diffable against a regenerated one — it is 95 KB of wizard
# output that nobody edits by hand. CCC's own overrides go in the rc below, AFTER this is sourced, so
# this file never has to be touched to change one of them.
P10K_CONFIG = (Path(__file__).parent / "zsh" / "p10k.zsh").read_text()


# What p10k's context segment says instead of `<guise>@<arm>`. The account a User logs into is their
# **Guise**, so the honest default renders as `ccc-u60002@<arm>` — a uid nobody knows, for an identity
# CONTEXT.md is explicit is not an account anyone logs into. The hostname half is left alone — it is the
# Arm's real name (apptainer is passed no `--uts`, so the Pod shares the host's UTS namespace).
CONTEXT_LABEL = "tidepool"


def render() -> str:
    """`/etc/zsh/zshrc` for one **Tide Pool** session."""
    return _ZSHRC_TMPL.format(
        omz=OMZ_DIR, state=POD_ZSH_STATE, p10k=POD_P10K,
        user_rc=POD_USER_ZSHRC,
        context=CONTEXT_LABEL,
    )


# NOTE `{` and `}` are doubled throughout — this is a `str.format` template and zsh is full of braces.
_ZSHRC_TMPL = """\
# Rendered by the Head for this session (ccc_head_api.zshrc) and written into the Pod's overlay at
# boot. Editing it here changes nothing that outlives this shell — the file is recreated from scratch
# every time a Tide Pool starts. {user_rc} is read after this one and wins; it is yours to edit, and it
# is not in your home on purpose (see POD_ZDOTDIR in ccc_head_api.zshrc).

# Off, and load-bearing rather than a preference. /etc/profile is sourced before this file (see the
# bridge in cuttle.templates.BASE_DEF_TMPL) and prints the "kept when this session ends" notice, which
# instant prompt would flag as console output during initialization on every single login.
typeset -g POWERLEVEL9K_INSTANT_PROMPT=off

export ZSH={omz}
ZSH_THEME="powerlevel10k/powerlevel10k"

# A Pod lasts one session and the tree it would update is read-only squashfs. An update prompt at a
# Tide Pool login would be pure noise, every time.
zstyle ':omz:update' mode disabled

# Created BEFORE oh-my-zsh.sh is sourced, deliberately — omz falls back to $HOME/.cache for a cache
# dir that is not writable *yet*, and this module's docstring explains why that home is the one place
# none of this may land.
export ZSH_CACHE_DIR={state}/omz
export ZSH_COMPDUMP=$ZSH_CACHE_DIR/zcompdump
export HISTFILE={state}/history
export _ZO_DATA_DIR={state}/zoxide
mkdir -p $ZSH_CACHE_DIR {state}/zoxide

# powerlevel10k needs a gitstatusd binary and ships none: with nothing baked it downloads ~2 MB from
# GitHub at the first prompt of EVERY session (the cache it would keep dies with the Pod) and prints an
# initialization error on any Arm with no egress. The binary is vendored into the image instead, at
# `gitstatus/usrbin/gitstatusd-linux-x86_64` — upstream's own bring-your-own-daemon slot, which
# gitstatus/install checks before it considers the cache or the network. That is why nothing here points
# at it: found by name, configured by nobody.
#
# Two dead ends are worth not re-walking, both measured rather than reasoned. Setting GITSTATUS_DAEMON to
# an absolute path looks like the direct way to say this and fails: it makes install take its version from
# `build.info` (v1.5.5) rather than `install.info` (v1.5.4, the release that actually has a linux-x86_64
# prebuilt), the plugin then launches the daemon as `gitstatusd -G v1.5.5`, the binary fails its own
# version check, and the prompt comes up "gitstatus failed to initialize". And GITSTATUS_AUTO_INSTALL=0
# looks like the way to forbid the download: it becomes install's `-n` flag, which skips the entire block
# that locates an existing daemon, so it breaks resolution instead of constraining it.
#
# The cache dir still gets pointed at scratch. Nothing should ever write there — usrbin wins first — but
# a download that did happen would otherwise land in the 64 MB overlay or the shared home.
export GITSTATUS_CACHE_DIR={state}/gitstatus

plugins=(git zoxide sudo zsh-syntax-highlighting zsh-autosuggestions you-should-use colored-man-pages extract)

source $ZSH/oh-my-zsh.sh

eval "$(zoxide init zsh)"

source {p10k}

# After the p10k config, so that file stays a verbatim copy of what `p10k configure` wrote. An SSH
# session takes p10k's REMOTE context class — a Tide Pool is always one — so this is the template that
# actually renders; the DEFAULT class is blanked in the config and never applies here.
typeset -g POWERLEVEL9K_CONTEXT_{{REMOTE,REMOTE_SUDO}}_TEMPLATE='{context}@%m'
"""
