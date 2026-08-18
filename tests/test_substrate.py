"""The **Substrate** declaration: what a Spawn may ask the archive for, and what it may not.

Pure with remote consequences (CONTRIBUTING.md) — every name that leaves `read` becomes an argument
to `apt-get` on a donor's box, so the refusals matter more than the acceptances. Three things worth
pinning: only bare package names survive, the file stays inside the project, and declaring nothing
is ordinary rather than an error (the one place a Substrate differs from a manifest).
"""

import pytest

from ccc_head_api import substrate


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
