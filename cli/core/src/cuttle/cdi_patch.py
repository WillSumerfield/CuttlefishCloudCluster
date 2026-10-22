#!/usr/bin/python3 -I
"""ccc-cdi-patch — make NVIDIA's CDI spec readable by Podman 4.9.

Installed at /usr/local/sbin/ccc-cdi-patch by `cuttle setup`, run by ccc-cdi-patch.path whenever
nvidia-cdi-refresh.service regenerates /var/run/cdi/nvidia.yaml (boot, driver change).

Podman 4.9 reads CDI ≤0.6: nvidia-ctk 1.20 writes 0.7 plus `additionalGids` (a 0.7 field). Dropping
both is all it takes — the gids only matter for /dev/dri, which CUDA never opens. Writes only on a
change, so its own write does not re-trigger the path unit forever.
"""

import re
import sys
from pathlib import Path

SPEC = Path("/var/run/cdi/nvidia.yaml")


def patch(text: str) -> str:
    out, skip = [], None
    for ln in text.splitlines(keepends=True):
        indent = len(ln) - len(ln.lstrip())
        if skip is not None:
            if ln.strip() and (indent > skip or ln.lstrip().startswith("- ") and indent == skip):
                continue
            skip = None
        if ln.lstrip().startswith("additionalGids:"):
            skip = indent
            continue
        out.append(ln)
    return re.sub(r"^cdiVersion: .*$", "cdiVersion: 0.6.0", "".join(out), count=1, flags=re.M)


def main(path: Path = SPEC) -> int:
    if not path.exists():
        return 0
    text = path.read_text()
    new = patch(text)
    if new != text:
        tmp = path.with_suffix(".ccc-tmp")
        tmp.write_text(new)
        tmp.replace(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else SPEC))
