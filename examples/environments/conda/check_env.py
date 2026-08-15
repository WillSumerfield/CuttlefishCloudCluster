"""What Python did this Spawn actually get?

Four lines, printed and written to results/env.txt:

  interpreter  — /opt/ccc/env/... means an Environment was built; anything else is the base
                 image's untouched Python.
  python       — the VERSION that env was built against. A declared `requires-python` is
                 honoured exactly: the Environment fetches that interpreter rather than
                 settling for whatever the cluster's image happens to ship.
  pyfiglet     — present only if a manifest was found and built. Same 1.0.4 on PyPI and
                 conda-forge, so uv/conda examples declare the same thing and differ only in
                 build method.
  torch        — the base image ships it, and an Environment inherits NOTHING from the base
                 image. So it is MISSING in every example here that builds one, whichever
                 file declared it. That is the point: what you declared is what you get.
"""

import sys
from pathlib import Path

lines = [f"interpreter: {sys.executable}",
         f"python: {'.'.join(str(n) for n in sys.version_info[:3])}"]
for name in ("pyfiglet", "torch"):
    try:
        mod = __import__(name)
        lines.append(f"{name}: {getattr(mod, '__version__', 'present')}")
    except ImportError:
        lines.append(f"{name}: MISSING")

report = "\n".join(lines)
print(report)
# An ordinary relative path — the folder this script would write to anywhere. Submit with
# `-o results` and CCC keeps exactly this directory; anything written outside a declared output
# dies with the scratch dir. Nothing here knows it is running on a cluster.
out = Path("results")
out.mkdir(exist_ok=True)
(out / "env.txt").write_text(report + "\n")
