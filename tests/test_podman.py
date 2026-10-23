"""podman.py: registry, per-Arm credentials, image build/record, teardown — system calls faked."""

import json
import subprocess
from pathlib import Path

import pytest

from cuttle import config, podman, teardown, templates


@pytest.fixture
def box(monkeypatch, tmp_path):
    """Root files land in a dict; every `run` is recorded and answers per `answers`."""
    files: dict[Path, tuple[str, str]] = {}
    ran: list[list[str]] = []
    answers: dict[str, subprocess.CompletedProcess] = {}

    def write(path, content, mode="644"):
        files[Path(path)] = (content, mode)

    def run(cmd, **kw):
        cmd = [str(c) for c in cmd]
        ran.append(cmd)
        for key, ans in answers.items():
            if key in " ".join(cmd):
                return ans
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(podman, "run", run)
    monkeypatch.setattr(podman.ui, "say", lambda *a, **k: None)
    return write, files, ran, answers


class TestRegistry:
    def test_two_instances_one_read_only_on_the_mesh(self, box):
        write, files, ran, _ = box
        podman.install_registry(write, "100.99.0.1")
        push = files[config.REGISTRY_ETC / "push.yml"][0]
        pull = files[config.REGISTRY_ETC / "pull.yml"][0]
        assert "addr: 127.0.0.1:5000" in push and "readonly:\n      enabled: false" in push
        assert "addr: 100.99.0.1:5000" in pull and "readonly:\n      enabled: true" in pull
        for role in ("push", "pull"):
            q = files[config.QUADLET_DIR / f"{config.REGISTRY_SERVICE}-{role}.container"][0]
            assert f"Image={config.REGISTRY_IMAGE}" in q and f"/{role}.htpasswd:" in q
            assert "ccc-ca.key" not in q       # the leaf only, never the CA key
            assert ["systemctl", "restart", f"{config.REGISTRY_SERVICE}-{role}"] in ran

    def test_push_credential_is_minted_once(self, box):
        write, files, ran, answers = box
        answers["test -s"] = subprocess.CompletedProcess([], 1)    # none yet
        podman.install_registry(write, "100.99.0.1")
        auth, mode = files[config.REGISTRY_AUTH]
        assert mode == "600" and config.REGISTRY in json.loads(auth)["auths"]
        files.clear()
        answers["test -s"] = subprocess.CompletedProcess([], 0)    # already there
        podman.install_registry(write, "100.99.0.1")
        assert config.REGISTRY_AUTH not in files

    def test_pull_cred_mint_and_revoke(self, box):
        write, files, ran, answers = box
        podman.mint_pull_cred(write, "willpc")
        assert any(c[:2] == ["htpasswd", "-bB"] and "arm-willpc" in c for c in ran)
        creds, mode = files[config.REGISTRY_CREDS]
        assert mode == "600" and "willpc" in json.loads(creds)
        assert ["chown", "munge:munge", str(config.REGISTRY_CREDS)] in ran
        answers["cat"] = subprocess.CompletedProcess([], 0, stdout=creds)
        podman.revoke_pull_cred(write, "willpc")
        assert ["htpasswd", "-D", str(config.REGISTRY_PULL_HTPASSWD), "arm-willpc"] in ran
        assert json.loads(files[config.REGISTRY_CREDS][0]) == {}


class TestBuildImages:
    def test_digests_recorded_and_existing_images_not_rebuilt(self, box, monkeypatch, tmp_path):
        write, files, ran, answers = box
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "uv").write_text("uv")
        (tools / "micromamba").write_text("mm")
        monkeypatch.setattr(config, "ENV_TOOLS_DIR", tools)
        answers["image exists"] = subprocess.CompletedProcess([], 0)    # both already built

        def push(cmd, **kw):
            cmd = [str(c) for c in cmd]
            ran.append(cmd)
            if cmd[1] == "push":
                Path(cmd[cmd.index("--digestfile") + 1]).write_text("sha256:" + "d" * 64)
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        monkeypatch.setattr(podman, "run", push)
        images = podman.build_images(write)
        assert not any(c[1] == "build" for c in ran)
        assert images == {n: f"{config.REGISTRY}/ccc/{n}@sha256:{'d' * 64}" for n in ("base", "bench")}
        assert json.loads(files[config.IMAGES_RECORD][0]) == images


class TestLauncherAndCdi:
    def test_launcher_and_sudoers(self, box):
        write, files, ran, _ = box
        podman.install_launcher(write)
        assert files[config.CCC_LAUNCH_BIN][1] == "755"
        assert files[config.CCC_LAUNCH_BIN][0].startswith("#!/usr/bin/python3 -I")
        rule, mode = files[config.SUDOERS_CCC_LAUNCH]
        assert mode == "440" and f"%{config.GUISE_GROUP} ALL=(root) NOPASSWD: " in rule
        assert rule.rstrip().endswith("^[0-9]+$")

    def test_cdi_watches_the_file_nvidia_regenerates(self, box):
        write, files, ran, _ = box
        podman.install_cdi(write)
        assert f"PathChanged={config.CDI_SPEC}" in files[config.CDI_PATCH_PATH_UNIT][0]
        assert [str(config.CDI_PATCH_BIN), str(config.CDI_SPEC)] in ran


class TestTeardown:
    def test_images_kept_without_all(self, box, monkeypatch):
        _, _, ran, _ = box
        monkeypatch.setattr(podman.shutil, "which", lambda b: "/usr/bin/podman")
        podman.remove(all_=False)
        assert not any("rmi" in c for c in ran)
        assert not any(str(config.REGISTRY_DATA) in c for c in ran)
        podman.remove(all_=True)
        assert any("rmi" in c for c in ran)
        assert any(str(config.REGISTRY_DATA) in c for c in ran)

    def test_containers_go_before_the_package_purge(self, monkeypatch):
        """podman.remove needs the podman the purge takes."""
        order = []
        for name in ("revoke_session", "stop_arm_daemons", "_leave_ccc_mesh", "remove_slurm_config",
                     "remove_munge_key", "remove_mantle", "remove_licence_account", "remove_guises",
                     "untrust_and_unname", "remove_repos", "remove_user_dirs", "remove_arm_state",
                     "remove_env_tools", "prune_opt_dir", "_summarise"):
            monkeypatch.setattr(teardown, name, lambda *a, **k: None)
        monkeypatch.setattr(teardown, "remove_containers", lambda all_: order.append("containers"))
        monkeypatch.setattr(teardown, "purge_packages", lambda: order.append("purge"))
        monkeypatch.setattr(teardown.ui, "result", lambda *a, **k: None)
        teardown._teardown_arm()
        assert order == ["containers", "purge"]


def test_epilog_reaps_a_killed_jobs_container():
    e = templates.render_epilog()
    assert f'podman rm -f -t 0 "{config.JOB_CONTAINER_PREFIX}$SLURM_JOB_ID"' in e
    assert "nsenter --mount=/proc/1/ns/mnt -- podman rm" in e
    assert 'slurmstepd.scope/job_"$SLURM_JOB_ID"' in e and "-exec rmdir {} \\;" in e
    assert f'rm -rf "{config.JOBS_DIR}/$SLURM_JOB_ID"' in e


class TestImageHygiene:
    def test_arm_prunes_only_unrecorded_digests(self, box):
        _, _, ran, answers = box
        answers["{{.ID}} {{.Digest}}"] = subprocess.CompletedProcess(
            [], 0, stdout="aaa sha256:keep\nbbb sha256:old\nccc sha256:older\n")
        podman.prune_images({"base": f"{config.REGISTRY}/ccc/base@sha256:keep"})
        assert ["podman", "rmi", "bbb", "ccc"] in ran

    def test_arm_with_nothing_stale_removes_nothing(self, box):
        _, _, ran, answers = box
        answers["{{.ID}} {{.Digest}}"] = subprocess.CompletedProcess([], 0, stdout="aaa sha256:keep\n")
        podman.prune_images({"base": f"{config.REGISTRY}/ccc/base@sha256:keep"})
        assert not any("rmi" in c for c in ran)

    def test_head_keeps_its_store_under_the_same_tag(self, box):
        _, _, ran, answers = box
        answers["{{.Repository}}"] = subprocess.CompletedProcess(
            [], 0, stdout=f"{config.REGISTRY}/ccc/base:{config.IMAGE_TAG}\n{config.REGISTRY}/ccc/bench:<none>\n")
        podman.wipe_old_release()
        assert not any("rmi" in c or str(config.REGISTRY_DATA) in c for c in ran)

    def test_head_wipes_images_and_store_on_a_tag_change(self, box):
        _, _, ran, answers = box
        answers["{{.Repository}}"] = subprocess.CompletedProcess(
            [], 0, stdout=f"{config.REGISTRY}/ccc/base:1.9\n")
        answers["{{.ID}}"] = subprocess.CompletedProcess([], 0, stdout="aaa\n")
        podman.wipe_old_release()
        assert ["podman", "rmi", "-f", "aaa"] in ran
        assert ["rm", "-rf", str(config.REGISTRY_DATA), str(config.IMAGES_RECORD)] in ran
