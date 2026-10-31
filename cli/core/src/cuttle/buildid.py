"""Which `cuttle` build this is: a hash of the package's source files.

The Head is the source of builds (`dist.py`). A wheel carries its hash in `cuttle/BUILD`, stamped by
`cli/core/hatch_build.py` with `source_hash` below; a checkout (the Head's editable install, or a
container's copy of it) has no stamp and hashes itself. Two installs with the same hash run the same
code. Stdlib only: the build hook loads this file directly.
"""

import hashlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
STAMP = PACKAGE / "BUILD"


def source_hash(root: Path) -> str:
    """Every file under `root` but bytecode and the stamp itself, path and content, in a fixed order."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or "__pycache__" in rel.parts or rel == Path("BUILD"):
            continue
        h.update(str(rel).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def stamped() -> bool:
    """A wheel install (stamped), as opposed to a checkout. Only a wheel install updates itself."""
    return STAMP.exists()


def current() -> str:
    return STAMP.read_text().strip() if stamped() else source_hash(PACKAGE)
