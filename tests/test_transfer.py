"""`cuttle upload/download`: cp-like rsync with clash refusal, and the forwarded socket that lets a shell
inside a Tidepool/Dive ask your machine to run it — confined to the folder you entered from.

The copy runs real rsync between local folders (the alias half is just rsync's `host:` syntax)."""

import shutil

import pytest
import typer

from cuttle import config, transfer

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")


def _copy(srcs, dest, force=False):
    out = []
    return transfer.copy([str(s) for s in srcs], str(dest), force, out.append), "".join(out)


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    return src


class TestTheCopy:
    def test_the_named_folder_lands_inside_dest_slash_or_not(self, tree, tmp_path):
        for i, name in enumerate([str(tree), f"{tree}/"]):
            dest = tmp_path / f"out{i}"
            rc, _ = _copy([name], dest)
            assert rc == 0 and (dest / "proj" / "sub" / "b.txt").read_text() == "b"

    def test_a_clash_refuses_and_lists_it(self, tree, tmp_path):
        dest = tmp_path / "out"
        (dest / "proj").mkdir(parents=True)
        (dest / "proj" / "a.txt").write_text("mine, different")
        rc, out = _copy([tree], dest)
        assert rc == transfer.CLASH_RC and "proj/a.txt" in out and "--force" in out
        assert (dest / "proj" / "a.txt").read_text() == "mine, different"
        assert not (dest / "proj" / "sub").exists()          # nothing at all was copied

    def test_force_overwrites(self, tree, tmp_path):
        dest = tmp_path / "out"
        (dest / "proj").mkdir(parents=True)
        (dest / "proj" / "a.txt").write_text("mine, different")
        rc, _ = _copy([tree], dest, force=True)
        assert rc == 0 and (dest / "proj" / "a.txt").read_text() == "a"

    def test_a_finished_rerun_is_not_a_clash(self, tree, tmp_path):
        dest = tmp_path / "out"
        assert _copy([tree], dest)[0] == 0
        assert _copy([tree], dest)[0] == 0

    def test_it_never_deletes(self, tree, tmp_path):
        dest = tmp_path / "out"
        (dest / "proj").mkdir(parents=True)
        (dest / "proj" / "extra").write_text("keep")
        assert _copy([tree], dest)[0] == 0 and (dest / "proj" / "extra").exists()

    def test_cuttleignore_is_honoured(self, tree, tmp_path):
        (tree / ".cuttleignore").write_text("sub/\n")
        dest = tmp_path / "out"
        assert _copy([tree], dest)[0] == 0
        assert (dest / "proj" / "a.txt").exists() and not (dest / "proj" / "sub").exists()

    def test_resumable_partials_are_kept_aside_not_at_the_name(self):
        a = transfer.rsync_argv(["h:proj"], ".")
        assert f"--partial-dir={transfer.PARTIAL_DIR}" in a and "--delete" not in " ".join(a)
        assert "--info=progress2" in a and a[-1] == "./"

    def test_container_paths_from_your_machine_are_from_home(self):
        assert [transfer.container_path(p) for p in ("proj", "~/proj", "~", "/tmp/x")] == \
            ["proj", "proj", ".", "/tmp/x"]


class TestTheEntryFolder:
    def test_inside_is_fine(self, tmp_path):
        assert transfer.confine(tmp_path, "a/b") == str(tmp_path / "a" / "b")
        assert transfer.confine(tmp_path, ".") == str(tmp_path)

    @pytest.mark.parametrize("bad", ["..", "../x", "/etc/passwd", "~/x", "a/../../x"])
    def test_escapes_are_refused(self, tmp_path, bad):
        with pytest.raises(transfer.Refused):
            transfer.confine(tmp_path / "root", bad)

    def test_a_symlink_out_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "out").symlink_to(tmp_path)
        with pytest.raises(transfer.Refused):
            transfer.confine(root, "out/x")


class TestTheLink:
    @pytest.fixture
    def fwd(self, ccc_home, monkeypatch, tmp_path):
        calls = []

        def fake_copy(srcs, dest, force, write):
            calls.append((srcs, dest, force))
            write("progress\n")
            return 0
        monkeypatch.setattr(transfer, "copy", fake_copy)
        monkeypatch.setattr(transfer, "inside", lambda: True)
        root = tmp_path / "entry"
        (root / "data").mkdir(parents=True)
        with transfer.Forward("lab", root) as f:
            monkeypatch.setenv(transfer.FWD_ENV, str(f.local))
            yield f, calls

    def test_download_from_inside_lands_under_the_entry_folder(self, fwd, capsys):
        f, calls = fwd
        transfer.download("/home/alice/proj", "data", None, False)
        assert calls == [(["lab:/home/alice/proj"], str(f.root / "data"), False)]
        assert "progress" in capsys.readouterr().out

    def test_upload_from_inside_goes_to_the_container_cwd(self, fwd, monkeypatch, tmp_path):
        f, calls = fwd
        monkeypatch.chdir(tmp_path)
        transfer.upload("data", None, None, True)
        assert calls == [([str(f.root / "data")], f"lab:{tmp_path}", True)]

    def test_an_escape_from_inside_is_refused_by_your_machine(self, fwd):
        with pytest.raises(typer.Exit):
            transfer.download("/x", "../../etc", None, False)
        assert fwd[1] == []

    def test_no_link_says_enter_via_cuttle(self, monkeypatch, capsys):
        monkeypatch.setattr(transfer, "inside", lambda: True)
        monkeypatch.delenv(transfer.FWD_ENV, raising=False)
        with pytest.raises(typer.Exit):
            transfer.download("x", None, None, False)
        assert "cuttle tidepool" in capsys.readouterr().err

    def test_dive_is_refused_inside(self, monkeypatch):
        monkeypatch.setattr(transfer, "inside", lambda: True)
        with pytest.raises(typer.Exit):
            transfer.upload("x", None, 5, False)

    def test_the_socket_is_private_and_forwarded_with_its_path(self, fwd):
        f, _ = fwd
        assert f.local.stat().st_mode & 0o777 == 0o600
        assert f.local.parent == config.CCC_HOME / "run"
        a = f.ssh_args()
        assert a[a.index("-R") + 1] == f"{f.remote}:{f.local}"
        assert f"SetEnv={transfer.FWD_ENV}={f.remote}" in a


class TestFromYourMachine:
    def test_download_targets_the_tidepool_alias(self, monkeypatch):
        calls = []
        monkeypatch.setattr(transfer, "inside", lambda: False)
        monkeypatch.setattr(transfer, "_target", lambda d: ("lab", "."))
        monkeypatch.setattr(transfer, "copy", lambda s, d, f, w: calls.append((s, d)) or 0)
        transfer.download("~/proj", None, None, False)
        assert calls == [(["lab:proj"], ".")]

    def test_upload_to_a_dive_defaults_to_its_main_folder(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(transfer, "inside", lambda: False)
        monkeypatch.setattr(transfer, "_target", lambda d: (f"lab-{d}", "proj"))
        monkeypatch.setattr(transfer, "copy", lambda s, d, f, w: calls.append((s, d)) or 0)
        (tmp_path / "x").write_text("x")
        transfer.upload(str(tmp_path / "x"), None, 58, False)
        assert calls == [([str(tmp_path / "x")], "lab-58:proj")]


class TestTheShells:
    def test_tidepool_and_dive_sshd_accept_the_link(self):
        from ccc_head_api import dive as head_dive
        from cuttle import templates
        for conf in (templates.TIDEPOOL_SSHD_CONFIG, head_dive.DIVE_INIT):
            assert "AcceptEnv CCC_FWD" in conf and "StreamLocalBindUnlink yes" in conf
