"""F3 — the Head signs the cuttle wheel it serves to Arms (ed25519), the Arm pins the Head's pubkey at
authorize, and the update job verifies the wheel before installing it. A signature is channel-
independent, so a Head impersonator on the mesh can't forge an install over the same enroll channel.

Three layers:
- the exact openssl commands dist (sign) and rollout (verify) use actually round-trip, and reject a
  tampered wheel / wrong key (the crypto contract);
- dist.build wires the sign step in with the right key and output path;
- the enrollment token carries the pubkey, and the update JOB fetches + verifies before install.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from cuttle import config, dist, enroll, rollout


def _have_openssl():
    return shutil.which("openssl") is not None


pytestmark = pytest.mark.skipif(not _have_openssl(), reason="openssl not installed")


# --- the crypto contract: dist's sign + rollout's verify, verbatim ---------------

@pytest.fixture
def signed_wheel(tmp_path):
    """A key, a wheel, and a detached signature made with the SAME command dist.build runs."""
    key, pub = tmp_path / "s.key", tmp_path / "s.pub"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
    wheel = tmp_path / "cuttle-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"pretend wheel contents " * 100)
    sig = Path(str(wheel) + ".sig")
    # exactly dist.build's signing invocation
    subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key),
                    "-in", str(wheel), "-out", str(sig)], check=True)
    return key, pub, wheel, sig


def _verify(pub, wheel, sig):
    """Exactly the rollout JOB's verify invocation; returns the openssl exit code."""
    return subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(pub),
                           "-rawin", "-in", str(wheel), "-sigfile", str(sig)],
                          capture_output=True, text=True).returncode


class TestCryptoContract:
    def test_a_genuine_wheel_verifies(self, signed_wheel):
        _key, pub, wheel, sig = signed_wheel
        assert _verify(pub, wheel, sig) == 0

    def test_a_tampered_wheel_is_rejected(self, signed_wheel, tmp_path):
        _key, pub, _wheel, sig = signed_wheel
        bad = tmp_path / "bad.whl"
        bad.write_bytes(b"pretend wheel contents " * 100 + b"!")   # one byte more
        assert _verify(pub, bad, sig) != 0

    def test_a_different_key_is_rejected(self, signed_wheel, tmp_path):
        _key, _pub, wheel, sig = signed_wheel
        other = tmp_path / "other.pub"
        k2 = tmp_path / "other.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(k2)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(k2), "-pubout", "-out", str(other)], check=True)
        assert _verify(other, wheel, sig) != 0


# --- dist.build wires in the signing step ----------------------------------------

class TestBuildSigns:
    def test_build_signs_the_wheel_with_the_rollout_key(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd and cmd[0] == "uv":                       # the wheel build: drop a fake wheel
                out = cmd[cmd.index("--out-dir") + 1]
                (Path(out) / "cuttle-9.9.9-py3-none-any.whl").write_bytes(b"w")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(dist, "run", fake_run)
        monkeypatch.setattr(dist, "ensure_sign_key", lambda: calls.append(("ensure_sign_key",)))
        monkeypatch.setattr(dist.buildid, "source_hash", lambda _p: "deadbeef")
        monkeypatch.setattr(dist.config, "CUTTLE_DIST_DIR", tmp_path / "dist")
        written = {}
        dist.build(lambda path, text, *a: written.__setitem__(path, text))

        sign = [c for c in calls if c[:2] == ["openssl", "pkeyutl"] and "-sign" in c]
        assert len(sign) == 1, "the wheel is signed exactly once"
        c = sign[0]
        assert "-rawin" in c and c[c.index("-inkey") + 1] == str(config.ROLLOUT_SIGN_KEY)
        assert c[c.index("-out") + 1].endswith("cuttle-9.9.9-py3-none-any.whl.sig")
        assert c[c.index("-in") + 1].endswith("/dist/cuttle-9.9.9-py3-none-any.whl")
        assert ("ensure_sign_key",) in calls                 # key ensured before signing

    def test_ensure_sign_key_is_idempotent_when_present(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            # `test -f <key>` returns 0 (present) so generation is skipped
            rc = 0 if cmd[:2] == ["test", "-f"] else 0
            return subprocess.CompletedProcess(cmd, rc, "", "")

        monkeypatch.setattr(dist, "run", fake_run)
        dist.ensure_sign_key()
        assert not any("genpkey" in c for c in calls), "an existing key is not regenerated"


# --- the token carries the pubkey; the JOB verifies before install ---------------

class TestTokenCarriesPubkey:
    def test_mint_token_includes_the_rollout_pubkey(self, monkeypatch):
        monkeypatch.setattr(enroll.settings, "load",
                            lambda: {"mesh": {"coordinator": "https://head:8443"}})
        monkeypatch.setattr(enroll.mesh, "tailscale_ip", lambda: "100.99.0.1")
        pub = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
        tok = enroll._mint_token("arm1", "authkey", "enrollsecret", pub)
        payload = enroll.decode_token(tok)
        assert payload["rollout_pubkey"] == pub
        assert payload["enroll_secret"] == "enrollsecret"


class TestJobVerifies:
    def _job(self):
        ns: dict = {}
        exec(rollout._SCRIPT, ns)
        return ns["JOB"] % ("http://h", "arm1", "new", str(config.ARM_UPDATER_RECORD))

    def test_pin_path_is_baked_in(self):
        assert f'PIN = "{config.ROLLOUT_PUBKEY_ARM}"' in self._job()

    def test_verify_happens_before_install(self):
        job = self._job()
        assert job.index("pkeyutl") < job.index('"install"'), "verify must precede uv tool install"
        assert "/cuttle/wheel.sig" in job and "-verify" in job

    def test_missing_pin_and_bad_sig_both_fail(self):
        job = self._job()
        assert "os.path.exists(PIN)" in job                  # no pinned key -> refuse
        assert "FAILED signature verification" in job        # bad signature -> refuse
