"""What Python did this Spawn actually get?

Three lines, printed and written to $CCC_OUT/env.txt, and between them they identify which of the
build paths ran:

  interpreter  — /opt/ccc/env/... means an Environment was built and bound; anything else is the
                 cluster base image's own Python, untouched.
  pyfiglet     — the declared package. Present only if a manifest was found AND built. Chosen because
                 it is the same 1.0.4 on PyPI and on conda-forge, so the uv and conda examples declare
                 the identical thing and differ only in how it is built.
  torch        — the base image ships it. Present through a uv build (--system-site-packages layers
                 on top of the image) and MISSING through a conda one (its own prefix, inheriting
                 nothing) — which is the difference between "uv layers" and "conda replaces".
"""

import os
import sys
from pathlib import Path

lines = [f"interpreter: {sys.executable}"]
for name in ("pyfiglet", "torch"):
    try:
        mod = __import__(name)
        lines.append(f"{name}: {getattr(mod, '__version__', 'present')}")
    except ImportError:
        lines.append(f"{name}: MISSING")

report = "\n".join(lines)
print(report)
# $CCC_OUT is the only path off the cluster — anything written elsewhere dies with the scratch dir.
Path(os.environ["CCC_OUT"], "env.txt").write_text(report + "\n")
