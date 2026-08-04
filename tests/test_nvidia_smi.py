"""`system.nvidia_smi()` resolves the binary regardless of who invoked it.

On WSL, nvidia-smi lives in /usr/lib/wsl/lib, on PATH only in an interactive login shell — an
srun step wouldn't find it by bare name. Falls back to the absolute WSL path so the check
actually verifies the GPU.
"""

from cuttle import config, system


class TestNvidiaSmiResolves:
    def test_on_path_is_used_verbatim(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        # is_wsl must not even be consulted when PATH already has it.
        monkeypatch.setattr(system, "is_wsl", lambda: (_ for _ in ()).throw(AssertionError("unused")))
        assert system.nvidia_smi() == "/usr/bin/nvidia-smi"

    def test_wsl_falls_back_to_the_absolute_path(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: None)   # not on PATH
        monkeypatch.setattr(system, "is_wsl", lambda: True)
        monkeypatch.setattr(system.os.path, "exists", lambda p: p == config.WSL_NVIDIA_SMI)
        assert system.nvidia_smi() == config.WSL_NVIDIA_SMI

    def test_native_without_it_returns_bare_name_to_fail_loudly(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: None)
        monkeypatch.setattr(system, "is_wsl", lambda: False)
        assert system.nvidia_smi() == "nvidia-smi"

    def test_wsl_but_binary_missing_returns_bare_name(self, monkeypatch):
        """A WSL box whose driver shim isn't where we expect must still surface a real failure, not
        silently hand back a path that doesn't exist."""
        monkeypatch.setattr(system.shutil, "which", lambda name: None)
        monkeypatch.setattr(system, "is_wsl", lambda: True)
        monkeypatch.setattr(system.os.path, "exists", lambda p: False)
        assert system.nvidia_smi() == "nvidia-smi"
