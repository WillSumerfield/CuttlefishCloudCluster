"""A **Substrate** — the system packages a Spawn declares, laid into its **Pod** before anything else.

What `environments` is to Python, this is to everything under it. A dependency file cannot name
`libjpeg-dev` or `ffmpeg`, because those are not Python packages; but a wheel that has to compile
needs the first at build time and a script that shells out needs the second at run time. So the Spawn
declares them too, in a file in its own project, named at submit exactly as its manifest is.

**Decided here, installed there** — the same split `environments` makes, for the same reason. This
module reads the declaration, refuses anything malformed while a refusal is still free, and hands
back a list of names. `spawns._render_env_build` turns that into one `apt-get install` at the top of
the job's build block, and the Arm runs it inside the Pod against its own link to the archive.

**Names only, and that is a rule about the command line before it is a rule about taste.** Every line
here ends up as an argument to `apt-get` on a donor's box. `.deb` URLs, `-o` settings, PPAs and shell
metacharacters are all refused rather than quoted-and-hoped, so the worst a malformed Substrate can
do is fail to name a package. Version pins go with them: they are not *dangerous*, but `pkg=1:2.3-4`
pins to one archive snapshot on one release and breaks on the next Arm to be set up, which reads as
CCC losing the package rather than as the pin doing its job.

**Nothing here decides where a Spawn runs.** That is the line between a Substrate and an **Arm
Feature** (CONTEXT.md), and it holds because these packages come off the public archive: any Arm can
fetch them, so wanting them narrows the fleet not at all. A Feature is licence-gated or too heavy to
fetch, lives on particular Arms, and therefore decides **Eligibility**.

**A name nothing can install is a failed Spawn, not a refusal** — no archive can be consulted at
submit without a network call per submission against a package list that differs per Arm. Same terms
as a failed build, and it fails at the same moment, in the same block, with apt's own output in `err`.
"""

import re
from pathlib import Path

# Debian policy §5.6.1, and deliberately not one character looser. Lowercase alphanumerics plus `+`,
# `-` and `.`, at least two characters, starting alphanumeric. This is the whole of the validation:
# anything that is not a bare package name — a URL, a `pkg=version` pin, a flag, a shell metacharacter
# — fails to match and is named back to the submitter as the line it came from.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")


class SubstrateError(ValueError):
    """The declared Substrate cannot be read, decided at SUBMIT — a 4xx before any Spawn exists.

    A package that does not INSTALL is not this: that is decided by the archive, on the Arm, minutes
    later, and it ends the Spawn the way a failed build does.
    """


def read(project: Path, declared: str | None) -> list[str]:
    """The package names `declared` asks for, in file order, deduplicated. `[]` when it declares none.

    The format is the `apt.txt` convention repo2docker and Binder already established — one name per
    line, `#` comments, blank lines ignored — because a User who has written one has written this, and
    a file that already exists in their repo should work unchanged.

    Unlike a manifest, declaring is **optional** and `None` is the ordinary case: most work needs no
    system package at all, and there is no equivalent of "the Environment is too load-bearing to
    infer" to argue from. A file that resolves to no names — every line commented out — installs
    nothing rather than being refused, so a Substrate can be switched off in place.
    """
    rel = (declared or "").strip()
    if not rel:
        return []
    # The containment rule the manifest gets (`environments.detect`): this path is joined onto a directory the Head owns, so `../../etc`
    # must not be a way out of it.
    target = (project / rel).resolve()
    if not str(target).startswith(str(project.resolve()) + "/") or not target.is_file():
        raise SubstrateError(f"no such substrate in this project: {rel}")
    names: list[str] = []
    for n, raw in enumerate(target.read_text(errors="replace").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not NAME_RE.match(line):
            raise SubstrateError(
                f"{rel} line {n}: {line!r} is not a package name. A Substrate names packages and "
                f"nothing else — one per line, no versions, no URLs, no apt options")
        if line not in names:
            names.append(line)
    return names


def describe(names: list[str]) -> str:
    """The submit-time echo, beside `environments.describe`'s.

    Said for the same reason: a Substrate one directory too deep is not a build failure, so nothing
    downstream would mention it and the User meets it as a missing header after paying for a GPU.
    """
    return f"substrate: {', '.join(names)}" if names else "substrate: none declared"
