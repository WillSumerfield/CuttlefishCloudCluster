"""The **Substrate** declaration: what a Spawn may ask the archive for, and what it may not.

Pure with remote consequences (CONTRIBUTING.md) — every name that leaves `read` becomes an argument
to `apt-get` on a donor's box, so the refusals matter more than the acceptances. Three things worth
pinning: only bare package names survive, the file stays inside the project, and declaring nothing
is ordinary rather than an error (the one place a Substrate differs from a manifest).
"""

from pathlib import Path

import pytest

from ccc_head_api import db, environments, recipes, spawns, substrate

from test_cli_e2e import _spawn_id
from test_recipes import _META, _targz


def _project(tmp_path, **files):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


# --- declaring nothing -----------------------------------------------------
# Unlike a manifest, a Substrate is optional: most work needs no system package, and there is no
# "too load-bearing to infer" to argue from. Nothing is detected either — an `apt.txt` sitting in a
# repo is not by itself an instruction to the cluster.

class TestDeclaringNothing:
    def test_no_declaration_asks_for_nothing(self, tmp_path):
        assert substrate.read(tmp_path, None) == []

    def test_an_empty_declaration_asks_for_nothing(self, tmp_path):
        assert substrate.read(tmp_path, "   ") == []

    def test_a_file_sitting_there_unnamed_is_not_picked_up(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "ffmpeg\n"})
        assert substrate.read(proj, None) == []

    def test_a_file_that_names_nothing_installs_nothing(self, tmp_path):
        # Every line commented out switches a Substrate off in place. Refusing here would mean a User
        # cannot disable one without also editing the submit command.
        proj = _project(tmp_path, **{"apt.txt": "# ffmpeg\n\n   \n"})
        assert substrate.read(proj, "apt.txt") == []


# --- the format ------------------------------------------------------------
# repo2docker/Binder's `apt.txt`, unchanged, so a file a User already has works as it is.

class TestTheFormat:
    def test_one_name_per_line(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "ffmpeg\nlibjpeg-dev\n"})
        assert substrate.read(proj, "apt.txt") == ["ffmpeg", "libjpeg-dev"]

    def test_comments_blanks_and_trailing_comments_go(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "# editors\n\nffmpeg  # for the sweep\n\ngit\n"})
        assert substrate.read(proj, "apt.txt") == ["ffmpeg", "git"]

    def test_declaration_order_is_kept(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "zlib1g-dev\nffmpeg\nlibjpeg-dev\n"})
        assert substrate.read(proj, "apt.txt") == ["zlib1g-dev", "ffmpeg", "libjpeg-dev"]

    def test_a_name_asked_for_twice_is_installed_once(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "ffmpeg\ngit\nffmpeg\n"})
        assert substrate.read(proj, "apt.txt") == ["ffmpeg", "git"]

    def test_the_file_need_not_be_called_apt_txt(self, tmp_path):
        # Named at submit like a manifest is, so a User may keep several and point at one.
        proj = _project(tmp_path, **{"deps/system-cuda.txt": "libjpeg-dev\n"})
        assert substrate.read(proj, "deps/system-cuda.txt") == ["libjpeg-dev"]


# --- what is refused -------------------------------------------------------
# Each of these would otherwise reach `apt-get` as an argument. Refused at submit, where it costs
# nothing, rather than quoted and hoped for.

class TestOnlyBareNamesSurvive:
    @pytest.mark.parametrize("line", [
        "libjpeg-dev=8c-2ubuntu10",              # a version pin: one archive snapshot, one release
        "https://example.com/thing.deb",         # a .deb URL
        "ppa:deadsnakes/ppa",                    # a PPA
        "--reinstall ffmpeg",                    # an apt flag
        "ffmpeg; rm -rf /",                      # a shell metacharacter
        "ffmpeg git",                            # two names on one line
        "FFmpeg",                                # Debian names are lowercase
        "-o APT::Get::AllowUnauthenticated=true",
        "../../etc/passwd",
        "a",                                     # policy: at least two characters
    ])
    def test_it_is_not_a_package_name(self, tmp_path, line):
        proj = _project(tmp_path, **{"apt.txt": f"{line}\n"})
        with pytest.raises(substrate.SubstrateError):
            substrate.read(proj, "apt.txt")

    def test_the_refusal_names_the_line_it_came_from(self, tmp_path):
        proj = _project(tmp_path, **{"apt.txt": "ffmpeg\n\nlibjpeg-dev=8c\n"})
        with pytest.raises(substrate.SubstrateError, match="line 3"):
            substrate.read(proj, "apt.txt")


# --- containment -----------------------------------------------------------
# The path is joined onto a directory the Head owns, so it gets the rule a manifest and a declared
# output both get: it must resolve inside the project.

class TestItStaysInTheProject:
    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(substrate.SubstrateError, match="no such substrate"):
            substrate.read(tmp_path, "apt.txt")

    def test_a_directory_is_not_a_declaration(self, tmp_path):
        (tmp_path / "apt.txt").mkdir()
        with pytest.raises(substrate.SubstrateError, match="no such substrate"):
            substrate.read(tmp_path, "apt.txt")

    def test_it_cannot_climb_out(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("ffmpeg\n")
        proj = tmp_path / "proj"
        proj.mkdir()
        with pytest.raises(substrate.SubstrateError, match="no such substrate"):
            substrate.read(proj, "../outside.txt")

    def test_an_absolute_path_is_refused(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("ffmpeg\n")
        proj = tmp_path / "proj"
        proj.mkdir()
        with pytest.raises(substrate.SubstrateError, match="no such substrate"):
            substrate.read(proj, str(outside))


class TestTheEcho:
    def test_it_names_what_was_declared(self, tmp_path):
        assert substrate.describe(["ffmpeg", "git"]) == "substrate: ffmpeg, git"

    def test_declaring_none_says_so(self, tmp_path):
        assert substrate.describe([]) == "substrate: none declared"


# --- what the Arm is told to run -------------------------------------------
# The Head decides, the Arm installs — the same split `environments` makes. Nothing here runs apt;
# what there is to check is the shell rendered into the job script.

def _block(packages, declared=None, features=()):
    """The %%ENV_BUILD%% text for a Spawn declaring `packages`."""
    return spawns._render_env_build(declared, Path("/tmp/u"), list(features), [], [], [],
                                    "", list(packages))


class TestTheInstallIsRendered:
    def test_declaring_nothing_renders_no_block_at_all(self):
        assert _block([]) == ""

    def test_a_substrate_alone_is_enough_to_need_one(self):
        """A **Tide Pool** may decline a manifest and still want ffmpeg. Before this, a Spawn with no
        Environment and no Feature rendered an empty build block and the declaration went nowhere."""
        assert "apt-get install" in _block(["ffmpeg"])

    def test_every_name_goes_in_one_command(self):
        """apt resolves the set together; one call per name would re-run the solver each time and
        fetch a shared dependency more than once."""
        block = _block(["ffmpeg", "libjpeg-dev", "git"])
        installs = [ln for ln in block.splitlines() if "apt-get install" in ln]
        assert len(installs) == 1
        for name in ("ffmpeg", "libjpeg-dev", "git"):
            assert name in installs[0]

    def test_nothing_is_recommended_into_the_pod(self):
        """The image itself is built --no-install-recommends, and a Substrate that quietly dragged in
        a documentation set would be the one place that stopped being true."""
        assert "--no-install-recommends" in _block(["ffmpeg"])

    def test_it_answers_no_questions(self):
        assert " -y " in _block(["ffmpeg"])

    def test_a_name_the_archive_has_not_got_fails_the_spawn(self):
        """No archive can be consulted at submit — the answer differs per Arm and per day — so this
        ends the Spawn the way a failed build does, with apt's own output in `err`."""
        block = _block(["ffmpeg"])
        assert "ccc_build_or_die apt-get install" in block

    def test_the_failure_says_it_was_the_substrate(self):
        """`ccc_building` is what the failure message names. Blaming 'the environment' for an apt
        failure would send a User to edit the wrong file."""
        assert "ccc_building='the substrate: ffmpeg'" in _block(["ffmpeg"])

    def test_it_is_laid_down_before_the_environment_is_built(self):
        """The whole reason it is declared rather than installed once the Spawn is running: a wheel
        that has to compile needs its headers present WHILE uv syncs, not afterwards."""
        block = _block(["libjpeg-dev"], declared=environments.Manifest("uv-requirements",
                                                                      "requirements.txt"))
        lines = block.splitlines()
        apt = next(i for i, ln in enumerate(lines) if "apt-get install" in ln)
        uv = next(i for i, ln in enumerate(lines) if "requirements.txt" in ln)
        assert apt < uv

    def test_it_runs_in_the_pod_and_not_on_the_arm(self):
        """`ccc_build` is the wrapper that enters the Pod. A bare `apt-get` here would run as the
        **Guise** on the donor's own box, against the donor's own /usr."""
        block = _block(["ffmpeg"])
        assert "\napt-get" not in block


# --- the CLI, end to end ---------------------------------------------------
# Rides the same `/submit` the Dashboard does, so what these pin holds for both clients.

class TestTheDeclarationTravels:
    def test_a_spawn_declaring_none_is_unaffected(self, signed_in, fake_slurm, project):
        result = signed_in("run", "train.py", "--name", "n", "--time", "60", "--vram", "24",
                           "--output", "out", "--manifest", "requirements.txt")
        assert result.exit_code == 0, result.output
        assert "substrate" not in result.output

    def test_the_echo_names_what_was_declared(self, signed_in, fake_slurm, project):
        """Said while the User is still watching. A Substrate is what a build compiles against, and
        discovering it went nowhere costs a queue place, a card and a wall clock."""
        (project / "apt.txt").write_text("# system deps\nlibjpeg-dev\nffmpeg\n")
        result = signed_in("run", "train.py", "--name", "n", "--time", "60", "--vram", "24",
                           "--output", "out", "--manifest", "requirements.txt",
                           "--substrate", "apt.txt")
        assert result.exit_code == 0, result.output
        assert "substrate: libjpeg-dev, ffmpeg" in result.output

    def test_the_arm_is_told_to_install_it(self, signed_in, fake_slurm, project):
        (project / "apt.txt").write_text("ffmpeg\n")
        result = signed_in("run", "train.py", "--name", "n", "--time", "60", "--vram", "24",
                           "--output", "out", "--manifest", "requirements.txt",
                           "--substrate", "apt.txt")
        assert result.exit_code == 0, result.output
        script = (fake_slurm / "scripts" / f"{_spawn_id(result.output)}.sh").read_text()
        assert "apt-get install -y --no-install-recommends ffmpeg" in script

    def test_a_file_that_is_not_there_never_leaves_the_client(self, signed_in, fake_slurm, project):
        """Caught before the whole project is uploaded, exactly as a missing manifest is."""
        result = signed_in("run", "train.py", "--name", "n", "--time", "60", "--vram", "24",
                           "--output", "out", "--manifest", "requirements.txt",
                           "--substrate", "nope.txt")
        assert result.exit_code != 0
        assert "substrate 'nope.txt' was not found" in result.output

    def test_a_bad_line_is_refused_before_the_spawn_exists(self, signed_in, fake_slurm, project):
        """The Head's own check, against what actually arrived — .gitignore can drop a file that is
        right here, so the client's stat is a courtesy and this is the one that counts."""
        (project / "apt.txt").write_text("ffmpeg\nhttps://example.com/thing.deb\n")
        result = signed_in("run", "train.py", "--name", "n", "--time", "60", "--vram", "24",
                           "--output", "out", "--manifest", "requirements.txt",
                           "--substrate", "apt.txt")
        assert result.exit_code != 0
        assert "not a package name" in result.output


# --- frozen with the code --------------------------------------------------


class _RecipeRig:
    """`recipes.save`/`update_meta` against a throwaway Head state dir, with an `apt.txt` in the
    upload. Thin on purpose — test_recipes.py owns the shelf's own behaviour; this only asks whether
    the Substrate rides along with everything else that is frozen."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def _spool(self, body: bytes) -> Path:
        p = self.tmp_path / "upload.tar.gz"
        p.write_bytes(body)
        return p

    def save(self, **over):
        body = _targz({"train.py": b"print('hi')\n", "requirements.txt": b"",
                       "apt.txt": b"ffmpeg\n"})
        with db.connect() as conn:
            return recipes.save(conn, 1, {**_META, **over}, self._spool(body))

    def update_meta(self, **over):
        with db.connect() as conn:
            return recipes.update_meta(conn, 1, _META["name"], {**_META, **over})


@pytest.fixture
def recipe_rig(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    monkeypatch.setattr(spawns, "HEAD_STATE_DIR", tmp_path)
    with db.connect() as conn:
        db.add_user(conn, "alice")
    return _RecipeRig(tmp_path)


# A **Recipe** freezes the declaration, not the resolved packages: the file itself is in the tree the
# Recipe already holds, and a stored list would go stale the moment its owner edited what they can see.

class TestARecipeFreezesIt:
    def test_it_is_kept_and_read_back(self, recipe_rig):
        saved = recipe_rig.save(substrate="apt.txt")
        assert saved["substrate"] == "apt.txt"

    def test_declaring_none_is_the_ordinary_case(self, recipe_rig):
        assert recipe_rig.save()["substrate"] == ""

    def test_naming_a_file_the_upload_has_not_got_is_refused(self, recipe_rig):
        """The same gate the script and the manifest get, and it matters more here than for a Spawn: a
        Recipe frozen around a missing file fails every time it is ever run, long after the folder
        that would have fixed it is gone."""
        with pytest.raises(ValueError, match="substrate 'nope.txt' was not found"):
            recipe_rig.save(substrate="nope.txt")

    def test_a_settings_edit_cannot_point_it_at_a_file_that_is_not_there(self, recipe_rig):
        recipe_rig.save(substrate="apt.txt")
        with pytest.raises(ValueError, match="substrate 'nope.txt' was not found"):
            recipe_rig.update_meta(substrate="nope.txt")

    def test_a_settings_edit_can_drop_it(self, recipe_rig):
        """An empty declaration is how a Recipe stops needing system packages — there is no equivalent
        for the manifest, which is mandatory."""
        recipe_rig.save(substrate="apt.txt")
        assert recipe_rig.update_meta(substrate="")["substrate"] == ""
