"""What Python did this Spawn actually get?

Three lines, printed and written to $CCC_OUT/env.txt:

  interpreter  — /opt/ccc/env/... means an Environment was built; anything else is the base
                 image's untouched Python.
  pyfiglet     — present only if a manifest was found and built. Same 1.0.4 on PyPI and
                 conda-forge, so uv/conda examples declare the same thing and differ only in
                 build method.
  torch        — base image ships it. Present via uv (--system-site-packages layers on the
                 image), MISSING via conda (own prefix, inherits nothing) — "uv layers" vs
                 "conda replaces".
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
