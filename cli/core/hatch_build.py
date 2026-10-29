"""Stamp each wheel with its build hash (`cuttle/BUILD`), so an installed cuttle can tell whether it
matches the Head's build without hashing itself on every command. See src/cuttle/buildid.py."""

import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version == "editable":
            return   # an editable install is a checkout: it hashes itself
        ns: dict = {"__file__": str(Path(self.root) / "src" / "cuttle" / "buildid.py")}
        src = Path(self.root) / "src" / "cuttle"
        exec((src / "buildid.py").read_text(), ns)
        stamp = Path(tempfile.mkdtemp()) / "BUILD"
        stamp.write_text(ns["source_hash"](src) + "\n")
        build_data["force_include"][str(stamp)] = "cuttle/BUILD"
