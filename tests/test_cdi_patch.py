"""ccc-cdi-patch: nvidia-ctk's 0.7 CDI spec → one Podman 4.9 reads."""

from cuttle.cdi_patch import main, patch

SPEC = """\
---
cdiVersion: 0.7.0
kind: nvidia.com/gpu
devices:
    - name: "0"
      containerEdits:
        deviceNodes:
            - path: /dev/nvidia0
              gid: 44
        additionalGids:
            - 44
            - 992
    - name: all
      containerEdits:
        additionalGids:
            - 44
        deviceNodes:
            - path: /dev/nvidia0
containerEdits:
    env:
        - NVIDIA_VISIBLE_DEVICES=void
"""


def test_patch_strips_gids_and_sets_version():
    out = patch(SPEC)
    assert "additionalGids" not in out and "- 992" not in out
    assert "cdiVersion: 0.6.0" in out
    assert "gid: 44" in out                      # a deviceNode's own gid is a 0.6 field — kept
    assert "    - name: all" in out and "            - path: /dev/nvidia0\n" in out
    assert out.endswith("NVIDIA_VISIBLE_DEVICES=void\n")


def test_patch_idempotent():
    once = patch(SPEC)
    assert patch(once) == once


def test_main_writes_only_on_change(tmp_path):
    f = tmp_path / "nvidia.yaml"
    f.write_text(patch(SPEC))
    mtime = f.stat().st_mtime_ns
    main(f)
    assert f.stat().st_mtime_ns == mtime
    f.write_text(SPEC)
    main(f)
    assert f.read_text() == patch(SPEC)


def test_temp_file_is_per_process_and_leaves_nothing(tmp_path, monkeypatch):
    """setup and the .path unit can run it at once: a shared temp name races on the rename."""
    spec = tmp_path / "nvidia.yaml"
    spec.write_text("cdiVersion: 0.8.0\n")
    import cuttle.cdi_patch as cp
    seen = []
    real = cp.Path.replace
    monkeypatch.setattr(cp.Path, "replace", lambda self, t: seen.append(self.name) or real(self, t))
    assert main(spec) == 0
    assert seen == [f".nvidia.yaml.{cp.os.getpid()}.ccc-tmp"]
    assert [p.name for p in tmp_path.iterdir()] == ["nvidia.yaml"]
