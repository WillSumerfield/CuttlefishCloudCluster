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
from ccc_head_api import ratelimit
from ccc_head_api import slurm
from ccc_head_api import tidepool as head_tidepool
from cuttle import api, auth, config, settings, ui
from cuttle import ccc_tidepool as cli_tidepool
from cuttle.main import app as cli_app

USERS = {"alice": "hunter2", "root": "toor"}  # root is the Admin
ADMINS = {"root"}


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    """The login throttle is process-global state (see ratelimit.py's module docstring) — every test
    in this suite shares one Python process, so without this a test late in the run could inherit
    another test's failed-login count and get a surprise 429."""
    ratelimit.reset()
    yield


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


@pytest.fixture(autouse=True)
def _cluster_name(monkeypatch, tmp_path_factory):
    """Every box in the suite is in a cluster named `lab` (`ccc.lab`, alias `lab`). Autouse: the domain,
    registry and aliases are all read from this one file, by the CLI, both root helpers and the Head API."""
    from ccc_head_api import enroll as head_enroll
    from ccc_head_api import tidepool as head_tp
    from cuttle import ccc_launch, ccc_tidepool
    f = tmp_path_factory.mktemp("etc") / "cluster-name"
    f.write_text("lab\n")
    for mod in (config, ccc_launch, ccc_tidepool, head_tp, head_enroll):
        monkeypatch.setattr(mod, "CLUSTER_NAME_FILE", f)
    return f


@pytest.fixture
def ccc_home(monkeypatch, tmp_path):
    """Redirect every piece of client-side state into tmp. A test that wrote a session into the real
    ~/.ccc would silently re-point the operator's own CLI at a test Head."""
    home = tmp_path / ".ccc"
    monkeypatch.setattr(config, "CCC_HOME", home)
    monkeypatch.setattr(config, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config" / "config.yaml")
    monkeypatch.setattr(config, "SESSION_FILE", home / "session")
    monkeypatch.setattr(config, "PINS_DIR", home / "pins")
    monkeypatch.setattr(config, "LOG_DIR", home / "logs")
    monkeypatch.setattr(config, "DIVE_DIR", home / "dives")
    # The User's own ~/.ssh, redirected for the same reason as everything above and then some: a test
    # that regenerated the operator's real ssh config would rewrite the file their whole machine's ssh
    # reads (sshconfig.refresh writes it in full).
    ssh = tmp_path / ".ssh"
    monkeypatch.setattr(config, "SSH_DIR", ssh)
    monkeypatch.setattr(config, "SSH_CONFIG", ssh / "config")
    monkeypatch.setattr(config, "SSH_CCC_CONFIG", ssh / "ccc.config")
    monkeypatch.setattr(config, "TIDEPOOL_KEY", ssh / "ccc_ed25519")
    monkeypatch.setattr(config, "TIDEPOOL_SSH_META", home / "tidepool-ssh.json")
    monkeypatch.setattr(config, "TIDEPOOL_KNOWN_HOSTS", home / "known_hosts")
    # Plain http, so tls pinning is a no-op: certificate handling is its own concern and not one the
    # CLI's behaviour should be coupled to in every test.
    settings.save({"head": "http://head"})
    return home


@pytest.fixture
def cli(monkeypatch, head, ccc_home):
    """Invoke real `cuttle` commands. Returns a callable: `cli("cancel", "58", "--yes")`.

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


@pytest.fixture(autouse=True)
def _head_state(monkeypatch, tmp_path_factory):
    """No test touches the live Head state.

    Autouse and unconditional: `tick.once` walks Head state on its own, so a test that never mentions
    it can still reach it."""
    # Tidepool keys: `tick.once` syncs ambient sessions into every User dir it finds there, so on a
    # real Head the suite would write into the live admin's keys dir.
    keys = tmp_path_factory.mktemp("tidepool-keys")
    for mod, attr in ((head_tidepool, "KEYS_ROOT"), (cli_tidepool, "KEYS_ROOT"),
                      (config, "TIDEPOOL_KEYS_ROOT")):
        monkeypatch.setattr(mod, attr, keys)


@pytest.fixture
def fake_slurm(monkeypatch, tmp_path):
    """Stand in for Slurm with throwaway executables on PATH (`squeue`, and a pass-through `sudo`), plus
    one admitted Arm."""
    state = tmp_path / "head-state"
    state.mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Two call shapes share this one stub: `squeue -h -j <id> -o %T` (slurm.state — no matching job
    # prints NOTHING, which is how the Head decides a Dive has left the queue) and `squeue --json`
    # (slurm.queue/dives — the Karma tick and the live queue, which still need valid empty JSON).
    (bin_dir / "squeue").write_text(
        '#!/bin/sh\ncase "$*" in\n  *--json*) echo \'{"jobs": []}\' ;;\n  *) exit 0 ;;\nesac\n')
    # A **Guise** owns every Dive, so the Head reaches Slurm through `sudo -n -u ccc-u<uid>`.
    (bin_dir / "sudo").write_text(
        '#!/bin/sh\nwhile [ "$1" = "-n" ]; do shift; done\n'
        'if [ "$1" = "-u" ]; then shift 2; fi\nexec "$@"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "vram_mb": 24564,
                                                 "admitted": True, "state": "idle",
                                                 "powers": [60.0], "gpu": 1, "gpus": 1}])
    return state


@pytest.fixture
def signed_in(cli):
    """A logged-in `alice`. The common precondition: nearly every command needs a Session."""
    result = cli("login", input=f"alice\n{USERS['alice']}\n")
    assert result.exit_code == 0, result.output
    return cli
