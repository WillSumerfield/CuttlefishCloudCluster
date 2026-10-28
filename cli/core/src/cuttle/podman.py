"""Podman plumbing every node shares: CDI, `ccc-launch`, the Head registry, the base + benchmark images.

Rootful Podman (Ubuntu's 4.9.3) on every node. Users never touch it: containers start only through
`ccc-launch` (cluster jobs, Dives) or, from phase 3, the Head's per-User Tidepool units.

**Images.** One frozen **base image** per release (Ubuntu 24.04 + tools + Python 3.12 + uv + nvcc, no
Python packages) and the **benchmark image** (`FROM base` + cuBLAS + a compiled SGEMM + bench.py). Both are built once, on
the Head, pushed to the Head registry, and recorded by digest in config.IMAGES_RECORD; every Arm pulls
both by digest at setup. A Dive's User layer is only valid on the exact base it was committed from,
which is why the base never changes inside a release (upgrade = clean install).

**Registry.** `registry:2`, two instances over one store, both on port config.REGISTRY_PORT:
- *push*: 127.0.0.1, one root-only credential — only the Head itself writes;
- *pull*: the mesh IP, read-only mode, one htpasswd user per Arm (minted at `arm authorize`, handed
  over at /enroll, dropped at `arm remove`).
Everything names images `ccc.<name>:5000/…`: the Head resolves the Cluster name to 127.0.0.1 (push
instance), an Arm to the Head's mesh IP (pull instance). TLS is the Head API's own leaf.
"""

import base64
import json
import secrets
import shutil
import tempfile
from pathlib import Path

from . import config, templates, ui
from .system import run

_HERE = Path(__file__).resolve().parent


# --- every node ----------------------------------------------------------------

def install_cdi(write_root_file) -> None:
    """NVIDIA's CDI spec, patched for Podman 4.9 now and after every regeneration.

    nvidia-container-toolkit's own nvidia-cdi-refresh.service writes /var/run/cdi/nvidia.yaml (at boot,
    on driver change). ccc-cdi-patch.path watches that file and patches it (cdi_patch.py)."""
    write_root_file(config.CDI_PATCH_BIN, (_HERE / "cdi_patch.py").read_text(), "755")
    write_root_file(config.CDI_PATCH_SERVICE_UNIT, templates.render_cdi_patch_service())
    write_root_file(config.CDI_PATCH_PATH_UNIT, templates.render_cdi_patch_path())
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", "nvidia-cdi-refresh.path",
         f"{config.CDI_PATCH_SERVICE}.path"], sudo=True)
    run(["systemctl", "start", "nvidia-cdi-refresh.service"], sudo=True)
    run([str(config.CDI_PATCH_BIN), str(config.CDI_SPEC)], sudo=True)


def install_launcher(write_root_file) -> None:
    """`ccc-launch` + its one sudoers rule: Guise-group members may run it with one numeric argument."""
    write_root_file(config.CCC_LAUNCH_BIN, (_HERE / "ccc_launch.py").read_text(), "755")
    write_root_file(config.SUDOERS_CCC_LAUNCH, templates.render_launch_sudoers(), "440")
    run(["install", "-d", "-m", "0755", str(config.JOBS_DIR)], sudo=True)


def write_registry_auth(write_root_file, user: str, password: str) -> None:
    """The root-only auth.json `podman pull/push --authfile` and ccc-launch read."""
    _write_auth(write_root_file, base64.b64encode(f"{user}:{password}".encode()).decode())


def _write_auth(write_root_file, tok: str) -> None:
    write_root_file(config.REGISTRY_AUTH,
                    json.dumps({"auths": {config.registry(): {"auth": tok}}}, indent=2) + "\n", "600")


def pull_images(images: dict) -> None:
    """**Arm.** Pull the base + benchmark images by digest (base layers shared: only cuBLAS + the bench are extra).
    A digest pull is content-verified by Podman; the inspect after it is the record that it landed."""
    for name, ref in sorted(images.items()):
        run(["podman", "pull", "--authfile", str(config.REGISTRY_AUTH), ref], sudo=True)
        got = run(["podman", "image", "inspect", "--format", "{{.Digest}}", ref], sudo=True,
                  capture_output=True, text=True).stdout.strip()
        if got != ref.split("@", 1)[1]:
            raise ui.abort(f"{name} image digest mismatch: wanted {ref}, got {got}")
        ui.say(f"  [muted]{name} image → {ref}[/muted]")


def _ccc_images(fmt: str) -> list[str]:
    return run(["podman", "images", "--digests", "--format", fmt, "--filter",
                f"reference={config.registry()}/ccc/*"], sudo=True, capture_output=True, text=True,
               check=False).stdout.splitlines()


def prune_images(images: dict) -> None:
    """**Arm.** Drop every `ccc/*` image the Head no longer records (old releases, stale rebuilds).
    An image a running container holds survives (`rmi` refuses it); the next setup gets it."""
    keep = {ref.split("@", 1)[1] for ref in images.values()}
    stale = sorted({i for i, d in (l.split() for l in _ccc_images("{{.ID}} {{.Digest}}"))
                    if d not in keep})
    if stale:
        run(["podman", "rmi", *stale], sudo=True, check=False)
        ui.say(f"  [muted]pruned {len(stale)} old image(s)[/muted]")


# --- Head only -------------------------------------------------------------------

def wipe_old_release() -> None:
    """A `ccc/*` image tagged other than config.IMAGE_TAG means a release change: every old image and
    the whole registry store go (nothing of the old release is pullable once images.json is rebuilt).
    Run before install_registry, which re-creates the store. Same-tag dev rebuilds are left alone."""
    tags = {l.rsplit(":", 1)[1] for l in _ccc_images("{{.Repository}}:{{.Tag}}")}
    if not tags - {config.IMAGE_TAG, "<none>"}:
        return
    ids = sorted({l for l in _ccc_images("{{.ID}}")})
    run(["systemctl", "stop", f"{config.REGISTRY_SERVICE}-push", f"{config.REGISTRY_SERVICE}-pull"],
        sudo=True, check=False)
    run(["podman", "rmi", "-f", *ids], sudo=True, check=False)
    run(["rm", "-rf", str(config.REGISTRY_DATA), str(config.IMAGES_RECORD)], sudo=True)
    ui.say(f"  [muted]release changed → dropped {len(ids)} old image(s) and the registry store[/muted]")


def install_registry(write_root_file, mesh_ip: str) -> None:
    """Both registry instances (Quadlets), their configs, and the Head's own push credential."""
    run(["install", "-d", "-m", "0700", str(config.REGISTRY_ETC)], sudo=True)
    run(["install", "-d", "-m", "0700", str(config.REGISTRY_DATA)], sudo=True)
    held = run(["cat", str(config.REGISTRY_AUTH)], sudo=True, capture_output=True, text=True,
               check=False).stdout.strip()
    if not held:
        pw = secrets.token_urlsafe(32)
        run(["htpasswd", "-cbB", str(config.REGISTRY_PUSH_HTPASSWD), config.REGISTRY_PUSH_USER, pw],
            sudo=True)
        write_registry_auth(write_root_file, config.REGISTRY_PUSH_USER, pw)
    else:
        # The entry is keyed by registry host, which carries the Cluster name: after a rename, move
        # the same credential (only its hash is in htpasswd) to the new host or every push is anonymous.
        auths = json.loads(held)["auths"]
        if list(auths) != [config.registry()]:
            _write_auth(write_root_file, next(iter(auths.values()))["auth"])
    run(["touch", str(config.REGISTRY_PULL_HTPASSWD)], sudo=True)
    run(["chmod", "600", str(config.REGISTRY_PUSH_HTPASSWD), str(config.REGISTRY_PULL_HTPASSWD)],
        sudo=True)
    for role, addr in (("push", "127.0.0.1"), ("pull", mesh_ip)):
        write_root_file(config.REGISTRY_ETC / f"{role}.yml",
                        templates.render_registry_config(addr, readonly=role == "pull"))
        write_root_file(config.QUADLET_DIR / f"{config.REGISTRY_SERVICE}-{role}.container",
                        templates.render_registry_quadlet(role))
    run(["podman", "pull", config.REGISTRY_IMAGE], sudo=True)
    run(["systemctl", "daemon-reload"], sudo=True)
    for role in ("push", "pull"):
        run(["systemctl", "restart", f"{config.REGISTRY_SERVICE}-{role}"], sudo=True)


def _restart_pull_registry() -> None:
    """htpasswd is read at start; a new or revoked Arm credential takes effect on restart."""
    run(["systemctl", "try-restart", f"{config.REGISTRY_SERVICE}-pull"], sudo=True, check=False)


def _load_creds() -> dict:
    r = run(["cat", str(config.REGISTRY_CREDS)], sudo=True, capture_output=True, text=True,
            check=False)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}


def _save_creds(write_root_file, creds: dict) -> None:
    write_root_file(config.REGISTRY_CREDS, json.dumps(creds, indent=2, sort_keys=True) + "\n", "600")
    run(["chown", f"{config.ENROLL_API_USER}:{config.ENROLL_API_USER}", str(config.REGISTRY_CREDS)],
        sudo=True)


def mint_pull_cred(write_root_file, arm: str) -> None:
    """A fresh pull-only credential for `arm`, stored where /enroll (as munge) hands it over."""
    pw = secrets.token_urlsafe(32)
    run(["htpasswd", "-bB", str(config.REGISTRY_PULL_HTPASSWD), f"arm-{arm}", pw], sudo=True)
    creds = _load_creds()
    creds[arm] = pw
    _save_creds(write_root_file, creds)
    _restart_pull_registry()


def revoke_pull_cred(write_root_file, arm: str) -> None:
    run(["htpasswd", "-D", str(config.REGISTRY_PULL_HTPASSWD), f"arm-{arm}"], sudo=True, check=False)
    creds = _load_creds()
    if creds.pop(arm, None) is not None:
        _save_creds(write_root_file, creds)
    _restart_pull_registry()


def _image_exists(ref: str) -> bool:
    return run(["podman", "image", "exists", ref], sudo=True, check=False).returncode == 0


def _build_push(name: str, tag: str, ctx: Path) -> str:
    """Build `ctx` into `<registry>/ccc/<name>:<tag>` if it is not here yet, push, return the digest ref."""
    local = f"{config.registry()}/ccc/{name}:{tag}"
    if _image_exists(local):
        ui.say(f"  [muted]{name} image exists ({local}) — not rebuilt (frozen per release; "
               f"`cuttle teardown --all` drops it)[/muted]")
    else:
        run(["podman", "build", "--pull=missing", "-t", local, str(ctx)], sudo=True)
    with tempfile.TemporaryDirectory() as tmp:
        digest_file = Path(tmp) / "digest"
        run(["podman", "push", "--authfile", str(config.REGISTRY_AUTH), "--digestfile",
             str(digest_file), local], sudo=True)
        digest = digest_file.read_text().strip()
    return f"{config.registry()}/ccc/{name}@{digest}"


def build_images(write_root_file) -> dict:
    """Build (if absent) + push the base and benchmark images; record their digest refs."""
    with tempfile.TemporaryDirectory() as tmp:
        base_ctx, bench_ctx = Path(tmp) / "base", Path(tmp) / "bench"
        base_ctx.mkdir(), bench_ctx.mkdir()
        (base_ctx / "Containerfile").write_text(templates.render_base_containerfile())
        for tool in ("uv", "micromamba"):
            shutil.copy2(config.ENV_TOOLS_DIR / tool, base_ctx / tool)
        shutil.copytree(config.ZSH_DATA_DIR / "oh-my-zsh", base_ctx / "oh-my-zsh", symlinks=True)
        (bench_ctx / "Containerfile").write_text(templates.render_bench_containerfile())
        (bench_ctx / "bench.py").write_text(templates.render_bench_py())
        (bench_ctx / "bench.cu").write_text(templates.BENCH_CU)
        images = {"base": _build_push("base", config.IMAGE_TAG, base_ctx),
                  "bench": _build_push("bench", config.IMAGE_TAG, bench_ctx)}
    write_root_file(config.IMAGES_RECORD, json.dumps(images, indent=2, sort_keys=True) + "\n")
    for name, ref in images.items():
        ui.say(f"  [muted]{name} → {ref}[/muted]")
    return images


def load_images() -> dict:
    return json.loads(config.IMAGES_RECORD.read_text())


# --- teardown ----------------------------------------------------------------------

def remove(all_: bool) -> None:
    """Every node: stop registry units (Head), drop job containers, CDI hook, launcher, creds.
    `all_` also drops CCC's images and the registry store (kept otherwise: frozen per release)."""
    if shutil.which("podman"):
        run(["systemctl", "stop", f"{config.REGISTRY_SERVICE}-push", f"{config.REGISTRY_SERVICE}-pull"],
            sudo=True, check=False)
        ids = run(["podman", "ps", "-aq", "--filter", f"name=^{config.JOB_CONTAINER_PREFIX}"],
                  sudo=True, capture_output=True, text=True, check=False).stdout.split()
        if ids:
            run(["podman", "rm", "-f", *ids], sudo=True, check=False)
        if all_:
            refs = run(["podman", "images", "--format", "{{.ID}}", "--filter",
                        f"reference={config.registry()}/ccc/*"], sudo=True, capture_output=True,
                       text=True, check=False).stdout.split()
            run(["podman", "rmi", "-f", *refs, config.REGISTRY_IMAGE], sudo=True, check=False)
    run(["systemctl", "disable", "--now", f"{config.CDI_PATCH_SERVICE}.path"], sudo=True, check=False)
    run(["rm", "-rf", str(config.QUADLET_DIR / f"{config.REGISTRY_SERVICE}-push.container"),
         str(config.QUADLET_DIR / f"{config.REGISTRY_SERVICE}-pull.container"),
         str(config.REGISTRY_ETC), str(config.REGISTRY_AUTH), str(config.REGISTRY_CREDS),
         str(config.CDI_PATCH_PATH_UNIT), str(config.CDI_PATCH_SERVICE_UNIT), str(config.CDI_PATCH_BIN),
         str(config.CCC_LAUNCH_BIN), str(config.SUDOERS_CCC_LAUNCH), str(config.JOBS_DIR)],
        sudo=True, check=False)
    if all_:
        run(["rm", "-rf", str(config.REGISTRY_DATA), str(config.IMAGES_RECORD)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)
