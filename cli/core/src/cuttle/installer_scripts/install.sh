#!/bin/sh
# CCC client installer. Drops the `cuttle` CLI on PATH via uv, and — if this bundle was built on a
# Head (`cuttle build-installer`) — points cuttle at that Head with its certificate pre-pinned, so
# the first `cuttle login` has nothing to type or confirm.
#
# Run from inside the unpacked bundle directory:  ./install.sh
#
# No hosted curl|sh: this script is built into the bundle and hand-carried, not fetched over the
# Head's self-signed cert. (astral.sh below is a real-CA host — a normal uv install, not that path.)
set -eu

here=$(cd "$(dirname "$0")" && pwd)

# 1. Ensure uv — the isolated tool installer. It puts each tool (and its deps) in its own venv, so
#    nothing leaks into the system Python and uninstalling is exact.
if ! command -v uv >/dev/null 2>&1; then
    echo "cuttle: uv not found — installing it..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv lands in ~/.local/bin; reach it for the rest of this run even before a new shell.
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Install cuttle + all its Python deps (from PyPI) into an isolated tool venv. --force so a
#    re-run cleanly replaces an older install rather than erroring.
wheel=$(ls "$here"/cuttle-*.whl 2>/dev/null | head -n1)
[ -n "$wheel" ] || { echo "cuttle: no cuttle-*.whl next to install.sh" >&2; exit 1; }
echo "cuttle: installing $(basename "$wheel") ..."
uv tool install --force "$wheel"

# 3. If head credentials were baked in, pre-point + pre-trust this client. build-installer laid the
#    files out at exactly their destination names, so this is a copy — no URL parsing in shell.
if [ -d "$here/head" ]; then
    mkdir -p "$HOME/.ccc/config" "$HOME/.ccc/pins"
    cp "$here/head/config.yaml" "$HOME/.ccc/config/config.yaml"
    chmod 600 "$HOME/.ccc/config/config.yaml"
    if ls "$here"/head/pins/*.pem >/dev/null 2>&1; then
        cp "$here"/head/pins/*.pem "$HOME/.ccc/pins/"
        chmod 600 "$HOME"/.ccc/pins/*.pem
    fi
    # 3a. The cluster name. The baked config points cuttle at a name, and nothing in CCC resolves
    #     names — so without this line the client is pointed somewhere it cannot reach. This is the
    #     one part of installing that needs root; it is also why a baked bundle asks for sudo at all.
    #     build-installer baked the finished line, marker and all, so nothing is reconstructed here.
    if [ -f "$here/head/hosts" ]; then
        echo "cuttle: adding the cluster name to /etc/hosts (needs sudo) ..."
        sudo sh -c "sed -i '/# cuttle\$/d' /etc/hosts && cat '$here/head/hosts' >> /etc/hosts"
    fi
    # 3b. Trust the cluster's CA, so the Dashboard loads without "your connection is not private".
    #     Three stores, because Linux has no one store every browser reads: the OS store (curl,
    #     python), a per-user NSS db (Chrome, Chromium, Edge, Brave), and a Firefox policy pointing
    #     back at the OS store. Mirrors system.trust_ccc_ca / trust_ccc_ca_browsers.
    if [ -f "$here/head/ccc-ca.crt" ]; then
        sudo install -D -m 644 "$here/head/ccc-ca.crt" /usr/local/share/ca-certificates/ccc-head.crt
        sudo update-ca-certificates >/dev/null
        nick=$(cat "$here/head/ca-nickname")
        if command -v certutil >/dev/null 2>&1; then
            mkdir -p "$HOME/.pki/nssdb"
            certutil -d "sql:$HOME/.pki/nssdb" -D -n "$nick" 2>/dev/null || true
            certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n "$nick" -i "$here/head/ccc-ca.crt"
        else
            echo "cuttle: no certutil — Chrome/Edge will keep warning until you run:"
            echo "cuttle:   sudo apt install libnss3-tools && cuttle setup --client"
        fi
        # Only when there is no policy file at all. This is a shared file that may already carry an
        # organisation's own settings, and a shell script has no business merging JSON — cuttle's own
        # path (system._firefox_enterprise_roots) does that properly, and `setup --client` will.
        if [ ! -e /etc/firefox/policies/policies.json ]; then
            sudo install -d /etc/firefox/policies
            sudo sh -c 'cat > /etc/firefox/policies/policies.json' <<'POLICY'
{"policies": {"Certificates": {"ImportEnterpriseRoots": true, "ccc": true}}}
POLICY
        else
            echo "cuttle: /etc/firefox/policies/policies.json already exists — left alone."
            echo "cuttle: run 'cuttle setup --client' if you want Firefox to trust the cluster too."
        fi
    fi
    echo "cuttle: installed, pointed at the cluster, its authority trusted."
    echo "cuttle: next -> cuttle login"
else
    # No baked address, so this box does not know a Head yet. Donating a GPU is `setup --arm`, which
    # points the box at the Head over the mesh as part of enrolling; a submit-only box uses --client
    # and is asked for the address. Named in that order because a generic bundle is what a remote Arm
    # gets (a baked one carries a LAN address it could not reach).
    echo "cuttle: installed."
    echo "cuttle: next -> cuttle setup --arm --token <token>   (donate this box's GPU)"
    echo "cuttle:      -> cuttle setup --client                (submit only)"
fi
