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
from ccc_head_api import environments
from ccc_head_api import main as head_main
from ccc_head_api import ratelimit
from ccc_head_api import siphons
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
from cuttle import api, auth, config, settings, ui, watch
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
def no_real_sweeper(monkeypatch):
    """No test ever forks the auto-download sweeper for real.

    Autouse and unconditional: `cuttle run` arms a watch entry now, and the root callback of EVERY
    command asks whether a sweeper should be running — so a test that never mentions downloading can
    still reach the fork. The child is a fresh interpreter, so it inherits none of the monkeypatching
    that keeps this suite off the operator's real `~/.ccc` and away from the real Head.

    The launches are recorded rather than dropped, so a test can assert one was attempted.
    """
    launched: list[None] = []
    monkeypatch.setattr(watch, "_launch", lambda: launched.append(None))
    return launched


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
    monkeypatch.setattr(config, "WATCH_DIR", home / "watch")
    monkeypatch.setattr(config, "OUTPUTS_DIR", home / "outputs")
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


@pytest.fixture(autouse=True)
def _siphon_dir(monkeypatch, tmp_path_factory):
    """Every **Siphon** a test opens lands in a throwaway directory.

    Autouse, and deliberately not opt-in: `submit()` mints one for every Spawn, so ANY test that
    reaches real submission would otherwise write rsync module files into the live /var/lib/ccc of
    whatever box the suite is running on. A test that wants to inspect them just re-patches this.
    """
    monkeypatch.setattr(siphons, "RSYNCD_DIR", tmp_path_factory.mktemp("rsyncd.d"))


@pytest.fixture(autouse=True)
def _head_state(monkeypatch, tmp_path_factory):
    """No test touches the live Head state dir, for the same reason as `_siphon_dir` above.

    Autouse and unconditional: `tick.once` walks the Spawn tree on its own (Recovery Window's
    register/reap pass), so a test that never mentions a directory can still reach one — and
    `recovery._evict` *unlinks*. On a box that is also a real Head, a simulated Spawn whose id
    collides with a real one reads, and deletes, that real Spawn's recovery copy (the Fake Cluster
    counts ids from 1, so this is not hypothetical).

    `fake_slurm` sets the same four attributes to its own tmp dir; a test using it simply overrides
    this, and both are undone at teardown."""
    state = tmp_path_factory.mktemp("head-state")
    for name in ("projects", "spawns", "scratch"):
        (state / name).mkdir()
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", state)
    monkeypatch.setattr(head_spawns, "PROJECTS_DIR", state / "projects")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", state / "spawns")
    monkeypatch.setattr(head_spawns, "SCRATCH_DIR", state / "scratch")


@pytest.fixture(autouse=True)
def _env_tools_exist(monkeypatch, tmp_path_factory):
    """Every Spawn declares an **Environment** now, so every test that submits one walks this path.

    One thing stands in: a TOOLS_DIR that exists, because `environments.require_tool` REFUSES a Spawn on
    a node with no uv/micromamba — without this, a suite running on a box that never ran `cuttle setup`
    would fail every submission test with a message about provisioning.

    Nothing needs to stand in for the BUILD itself any more. It runs on the Arm, inside the rendered job
    script, so a test that submits renders shell and executes none of it — there is no thread to race and
    no apptainer to require.

    Autouse and unconditional, for the same reason as `_head_state` above — a Spawn is submitted by
    tests that are about Karma, Siphons, watches and the CLI, none of which mention Environments.
    Autouse fixtures are set up before requested ones, so `tests/test_environments.py`'s own
    `no_tools`/`with_tools` still override this and stay in charge of the path they are about.
    """
    tools = tmp_path_factory.mktemp("ccc-tools")
    for name in ("uv", "micromamba"):
        (tools / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(environments, "TOOLS_DIR", tools)
    monkeypatch.setattr(environments, "UV_BIN", tools / "uv")
    monkeypatch.setattr(environments, "MICROMAMBA_BIN", tools / "micromamba")


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
    # A real Head always knows its mesh address (the unit injects it) and refuses to submit without
    # one, since a remote Arm would have nowhere to rsync from. Read from the environment at import,
    # which no test process sets.
    monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # sbatch --parsable prints just the job id. A counter file gives each Spawn a distinct, stable id.
    counter = tmp_path / "job-counter"
    counter.write_text("4200")
    (bin_dir / "sbatch").write_text(
        f'#!/bin/sh\nn=$(cat {counter}); n=$((n+1)); echo $n > {counter}; echo $n\n')
    # Two call shapes share this one stub: `squeue -h -j <id> -o %T` (spawns.active_state — no
    # matching job prints NOTHING, which is how the Head decides a Spawn is finished) and
    # `squeue --json` (slurm.queue/spawns — submit_checked's concurrent-Spawn cap + the Karma tick,
    # neither of which exists yet with any live jobs, but still needs valid empty JSON, not silence).
    (bin_dir / "squeue").write_text(
        '#!/bin/sh\ncase "$*" in\n  *--json*) echo \'{"jobs": []}\' ;;\n  *) exit 0 ;;\nesac\n')
    # A **Guise** owns every Spawn now (temp/spawn-isolation.md Phase 3), so the Head reaches Slurm
    # through `sudo -n -u ccc-u<uid>`. Faked at the same level as the binaries above, and for the same
    # reason: the real argv is still built by guises.as_owner, the real sbatch stub still runs, and the
    # account it was asked to become is recorded so a test can assert WHO submitted rather than just
    # that something did. Anything else would mean stubbing out the one line Phase 3 is about.
    (bin_dir / "sudo").write_text(
        f'#!/bin/sh\nwhile [ "$1" = "-n" ]; do shift; done\n'
        f'if [ "$1" = "-u" ]; then echo "$2" >> {tmp_path / "sudo-users"}; shift 2; fi\nexec "$@"\n')
    # The project lend (spawns._lend_project). Stubbed rather than left to the real tool so the suite
    # does not depend on `acl` being installed, or on tmp_path's filesystem carrying ACLs.
    (bin_dir / "setfacl").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path / "setfacl-calls"}\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "vram_mb": 24564,
                                                 "admitted": True, "state": "idle",
                                                 "powers": [60.0], "gpu": 1}])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb: f"vram{gb}")
    return state


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal project tree for `cuttle run` to upload, and a cwd to run it from.

    Carries a `requirements.txt` because every Spawn declares its **Environment** and both the CLI and
    the Head check the named file is really there — a project without one could not be submitted at
    all, which is not the state any test using this fixture is about."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "train.py").write_text("print('hello from a Spawn')\n")
    (root / "requirements.txt").write_text("")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def signed_in(cli):
    """A logged-in `alice`. The common precondition: nearly every command needs a Session."""
    result = cli("login", input=f"alice\n{USERS['alice']}\n")
    assert result.exit_code == 0, result.output
    return cli
