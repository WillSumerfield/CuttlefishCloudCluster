"""`cuttle logs` reaches stderr, not just stdout.

stdout and stderr are separate files on the Head (spawns.py writes `out` and `err`). A Spawn
that dies on a traceback can have empty stdout, so the non-follow read prints both sections and
`-f` interleaves them.
"""

from cuttle import spawns as cli_spawns


def _fake_streams(monkeypatch, out: str, err: str):
    """Point `_get_bytes` at in-memory stdout/stderr keyed by which endpoint the path ends in."""
    def fake(path: str, missing_ok: bool = False) -> bytes:
        return (err if path.endswith("/stderr") else out).encode()
    monkeypatch.setattr(cli_spawns, "_get_bytes", fake)


class TestNonFollowShowsBoth:
    def test_both_sections_are_printed(self, monkeypatch, capsys):
        _fake_streams(monkeypatch, out="epoch 1 loss=2.3\n", err="warning: lr high\n")
        cli_spawns.logs(77, follow=False)
        printed = capsys.readouterr().out
        assert "=== stdout ===" in printed and "epoch 1 loss=2.3" in printed
        assert "=== stderr ===" in printed and "warning: lr high" in printed

    def test_a_crash_with_empty_stdout_still_shows_the_traceback(self, monkeypatch, capsys):
        _fake_streams(monkeypatch, out="", err="RuntimeError: CUDA out of memory\n")
        cli_spawns.logs(77, follow=False)
        printed = capsys.readouterr().out
        assert "=== stderr ===" in printed and "CUDA out of memory" in printed

    def test_no_output_yet_is_stated(self, monkeypatch, capsys):
        _fake_streams(monkeypatch, out="", err="")
        cli_spawns.logs(77, follow=False)
        assert "no output" in capsys.readouterr().err   # a status notice, not data → stderr


class TestDrainTagsWholeLinesOnly:
    """`_drain` tags each complete line and holds a trailing partial one, so a prefix never splits a
    line mid-way. It returns the offset advanced to the last consumed newline."""

    def test_complete_lines_are_tagged(self, capsys):
        pos = cli_spawns._drain("out", "a\nb\n", 0)
        assert pos == 4
        assert capsys.readouterr().out == "[out] a\n[out] b\n"

    def test_a_partial_line_is_held(self, capsys):
        pos = cli_spawns._drain("err", "done\npartial", 0)
        assert pos == 5                       # consumed only up to the newline
        assert capsys.readouterr().out == "[err] done\n"

    def test_nothing_complete_emits_nothing(self, capsys):
        pos = cli_spawns._drain("out", "no newline yet", 0)
        assert pos == 0
        assert capsys.readouterr().out == ""
