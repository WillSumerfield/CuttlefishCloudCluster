"""The CLI end-to-end rig: real `cuttle` commands against a real Head, all in one process.

CCC's Dashboard gets no automated browser testing (a decision, not an omission — see ROADMAP Phase 6),
so **the CLI is this project's end-to-end surface**. A test here types what an operator types and
checks what they'd see, with nothing between the command and the Head API but the code that ships.

    cli("login", input="alice\\nhunter2\\n")
    cli("whoami")

What is real: argument parsing, the credential file, session minting, argon2 hashing, every Head API
route, and head.db. What is not: the network (an in-process `TestClient` stands in for the socket) and
Slurm (absent — `fake_slurm` puts throwaway `sbatch`/`squeue` on PATH).

The seam is `api.for_head`, the one place the CLI builds its HTTP client. Point it at the live app and
every command follows, because they all funnel through `cuttle/api.py`.
"""

import os

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
from cuttle import api, auth, config, settings, ui
from cuttle.main import app as cli_app

USERS = {"alice": "hunter2", "root": "toor"}  # root is the Admin
ADMINS = {"root"}


@pytest.fixture
def head(monkeypatch, tmp_path):
    """A Head API on a real, empty head.db, holding users who have already chosen their passwords
    (so the forced-rotation path stays out of tests that are not about it)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        for name, password in USERS.items():
            db.add_user(conn, name, is_admin=name in ADMINS)
            db.set_password(conn, name, password)
    return TestClient(head_main.app, base_url="http://head/api")


@pytest.fixture(autouse=True)
def no_stray_log(monkeypatch, tmp_path):
    """The run log is opened lazily and cached in a module global, so without this the *first* test
    to say anything opens a handle on the real ~/.ccc/logs and every later test writes through it —
    a fixture that redirects `config.LOG_DIR` cannot help once the file is already open.

    Autouse, because any test that reaches `ui.warn` writes a log, not only the ones that opt in."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ui, "_LOG", None)
    monkeypatch.setattr(ui, "_LOG_PATH", None)
    monkeypatch.setattr(ui, "_TROUBLE", [])


@pytest.fixture
def ccc_home(monkeypatch, tmp_path):
    """Redirect every piece of client-side state into tmp. A test that wrote a session into the real
    ~/.ccc would silently re-point the operator's own CLI at a test Head."""
    home = tmp_path / ".ccc"
    monkeypatch.setattr(config, "CCC_HOME", home)
    monkeypatch.setattr(config, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config" / "config.yaml")
    monkeypatch.setattr(config, "CREDENTIALS_FILE", home / "credentials")
    monkeypatch.setattr(config, "PINS_DIR", home / "pins")
    monkeypatch.setattr(config, "LOG_DIR", home / "logs")
    # Plain http, so tls pinning is a no-op: certificate handling is its own concern and not one the
    # CLI's behaviour should be coupled to in every test.
    settings.save({"head": "http://head"})
    return home


@pytest.fixture
def cli(monkeypatch, head, ccc_home):
    """Invoke real `cuttle` commands. Returns a callable: `cli("run", "train.py", "--time", "60")`.

    `for_head` is replaced rather than stubbed per-command, so anything the CLI does over HTTP —
    including code written later — lands on the in-process Head automatically.
    """
    def for_head(url=None, token=None):
        # per_request_timeout: TestClient runs the app in-process, where a wall-clock timeout is
        # meaningless, and warns if it is handed one.
        return api.Api(head, token=token, url="http://head", per_request_timeout=False)

    monkeypatch.setattr(api, "for_head", for_head)
    # auth caches one client per process; without this reset it would leak between tests.
    monkeypatch.setattr(auth, "_CLIENT", None)

    runner = CliRunner()

    def invoke(*args, input=None):
        # `input` is the answers a human would type, one per line. It reaches the prompts through
        # the `ui` seam rather than stdin: prompts read the keyboard directly now, so a CliRunner
        # feeding stdin would hang rather than answer. Same strings, same order, same assertions.
        if input is not None:
            ui.install_answers(ui.Answers(*input.rstrip("\n").split("\n")))
        try:
            return runner.invoke(cli_app, [str(a) for a in args], input=input)
        finally:
            ui.install_answers(None)

    return invoke


@pytest.fixture
def fake_slurm(monkeypatch, tmp_path):
    """Stand in for Slurm with two throwaway executables on PATH.

    Faking the *binaries* rather than `head_spawns.submit` keeps the interesting half real: the upload
    is genuinely unpacked, the job script genuinely rendered, and — the part that matters — the
    Attribution row and the Karma **Escrow** are genuinely written, in their real transaction. Stubbing
    `submit()` wholesale would skip all of that and leave the ledger empty, which is exactly the state
    these tests exist to check is *not* reachable.
    """
    state = tmp_path / "head-state"
    for name in ("projects", "spawns", "scratch"):
        (state / name).mkdir(parents=True)
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", state)
    monkeypatch.setattr(head_spawns, "PROJECTS_DIR", state / "projects")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", state / "spawns")
    monkeypatch.setattr(head_spawns, "SCRATCH_DIR", state / "scratch")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # sbatch --parsable prints just the job id. A counter file gives each Spawn a distinct, stable id.
    counter = tmp_path / "job-counter"
    counter.write_text("4200")
    (bin_dir / "sbatch").write_text(
        f'#!/bin/sh\nn=$(cat {counter}); n=$((n+1)); echo $n > {counter}; echo $n\n')
    # squeue with no matching job prints nothing == "Slurm has forgotten it", which is how the Head
    # decides a Spawn is finished.
    (bin_dir / "squeue").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "vram_mb": 24564,
                                                 "admitted": True, "state": "idle", "power": 60.0,
                                                 "gpu": 1}])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb: f"vram{gb}")
    return state


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal project tree for `cuttle run` to upload, and a cwd to run it from."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "train.py").write_text("print('hello from a Spawn')\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def signed_in(cli):
    """A logged-in `alice`. The common precondition: nearly every command needs a Session."""
    result = cli("login", input=f"alice\n{USERS['alice']}\n")
    assert result.exit_code == 0, result.output
    return cli
