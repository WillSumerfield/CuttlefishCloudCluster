"""`cuttle` inside the containers: one self-contained env per node at config.CONTAINER_CUTTLE.

Built inside a throwaway container of the base image, so its Python is the one every Tidepool and
Dive runs (user images are FROM base). The deps are pinned to what this box's own `cuttle` runs with;
the package itself is copied from this install, so the env always matches the CLI that built it.

Bind-mounted read-only into every Tidepool (ccc-tidepool) and, from each Arm's own copy, every Dive
(ccc-launch). Rebuilt by setup and `cuttle update`, and the contents swapped IN PLACE: a bind mount
pins the directory's inode, so a rename would leave running Tidepools on the old one forever.
"""

import importlib.metadata
import tempfile
from pathlib import Path

from . import config
from .system import run

_PKG = Path(__file__).resolve().parent


def requirements() -> str:
    """This install's own deps, exactly (cuttle itself is copied, not installed)."""
    pins = {f"{d.metadata['Name'].lower()}=={d.version}" for d in importlib.metadata.distributions()
            if d.metadata["Name"] and d.metadata["Name"].lower() not in ("cuttle", "pip", "setuptools")}
    return "".join(f"{p}\n" for p in sorted(pins))


def build_script(dest: Path) -> str:
    return f"""set -e
uv venv -q --python /usr/bin/python3 {dest}
uv pip install -q --no-cache --python {dest}/bin/python -r /ccc/requirements.txt
site=$({dest}/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
cp -r /ccc/src "$site/cuttle"
find "$site/cuttle" -name __pycache__ -prune -exec rm -rf {{}} +
{dest}/bin/python -m compileall -q "$site/cuttle"
mkdir -p {dest}/path
printf '#!{dest}/bin/python\\nimport sys\\nfrom cuttle.main import main\\nsys.exit(main())\\n' > {dest}/path/cuttle
chmod -R a+rX {dest}
chmod 755 {dest}/path/cuttle
"""


def build(base_ref: str) -> None:
    dest = config.CONTAINER_CUTTLE
    stage = dest.with_name(dest.name + ".new")
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "requirements.txt").write_text(requirements())
        (Path(td) / "src").mkdir()   # the mountpoint for the package, inside the ro /ccc
        Path(td).chmod(0o755)
        run(["rm", "-rf", str(stage)], sudo=True)
        run(["install", "-d", "-m", "0755", str(stage), str(dest)], sudo=True)
        run(["podman", "run", "--rm", "--pull=never", "--network", "host",
             "-v", f"{stage}:{dest}", "-v", f"{td}:/ccc:ro", "-v", f"{_PKG}:/ccc/src:ro",
             base_ref, "sh", "-c", build_script(dest)], sudo=True)
    # In place (see the module docstring): never `mv stage dest`.
    run(["rsync", "-a", "--delete", f"{stage}/", f"{dest}/"], sudo=True)
    run(["rm", "-rf", str(stage)], sudo=True)


def remove() -> None:
    run(["rm", "-rf", str(config.CONTAINER_CUTTLE)], sudo=True, check=False)
