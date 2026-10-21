"""`system.nvidia_smi()` resolves the binary off PATH, or hands back the bare name to fail loudly."""

from cuttle import system


class TestNvidiaSmiResolves:
    def test_on_path_is_used_verbatim(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        assert system.nvidia_smi() == "/usr/bin/nvidia-smi"

    def test_without_it_returns_bare_name_to_fail_loudly(self, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda name: None)
        assert system.nvidia_smi() == "nvidia-smi"
