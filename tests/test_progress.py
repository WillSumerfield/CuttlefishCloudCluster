"""What a progress line says about itself while it is happening.

What's tested: `ui.progress` latching a total that arrives after the line is already drawn. NOT
tested: what the bar looks like on a real terminal — `_Surface` renders to a Rich console and the
assertions here are on the Text it builds.
"""

import pytest

from cuttle import ui


# --- the line -----------------------------------------------------------------

@pytest.fixture
def drawn(monkeypatch) -> list[str]:
    """Every line `ui.progress` paints, as plain text. Captured at `_Surface.show`, which is the one
    point every one of the three drawing places goes through — so this reads what a terminal would
    without needing one."""
    lines: list[str] = []
    real = ui._Surface.show
    monkeypatch.setattr(ui._Surface, "show",
                        lambda self, renderable, height=1: lines.append(str(renderable)))
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "_REPAINT_S", 0.0)      # every update paints, so none is missed
    return lines


class TestTheLineLearnsItsTotalLate:
    """The size is read off a response header, which is not readable until the stream is already
    open — by which time the caller has drawn its first line. So the bar appears mid-transfer, and
    `update` takes the total as an optional second argument to make that possible."""

    def test_it_starts_without_a_bar_and_gains_one(self, drawn):
        with ui.progress("downloading") as update:
            update(0)
            first = drawn[-1]
            update(500_000, 1_000_000)
        assert "%" not in first
        assert "50%" in drawn[-1]

    def test_a_total_given_up_front_is_still_honoured(self, drawn):
        """Not every caller learns late — `progress(label, total=…)` still works, and the latch must
        not clobber it."""
        with ui.progress("building", total=1000) as update:
            update(250)
        assert "25%" in drawn[-1]

    def test_the_first_total_wins(self, drawn):
        """One fact about one transfer. A second value would be the Head changing its mind mid-
        stream, and the bar was already drawn against the first."""
        with ui.progress("downloading", total=1000) as update:
            update(500, 4000)
        assert "50%" in drawn[-1]

    def test_both_numbers_are_shown_not_just_a_percentage(self, drawn):
        '''"82%" of an unnamed quantity does not say whether the wait left is seconds or an hour,
        which is the question actually being asked.'''
        with ui.progress("downloading") as update:
            update(512 * 1024, 1024 * 1024)
        assert "50%" in drawn[-1]
        assert "512.0 KiB/1.0 MiB" in drawn[-1]

    def test_the_count_never_renders_past_the_total(self, drawn):
        """The numerator counts tar padding the Head did not weigh, so a small overshoot
        is normal. "1.1/1.0 GiB" would read as a bug in the transfer rather than in the arithmetic."""
        with ui.progress("downloading") as update:
            update(1100, 1000)
        assert "100%" in drawn[-1]
        assert "1000 B/1000 B" in drawn[-1]

    def test_without_a_total_it_stays_bytes_and_a_rate(self, drawn):
        """The honest rendering when nobody knows the denominator — an older Head, or a route that
        does not declare one."""
        with ui.progress("downloading") as update:
            update(4096)
        assert "%" not in drawn[-1]
        assert "4.0 KiB" in drawn[-1]

    def test_verbose_mode_takes_the_second_argument_too(self, monkeypatch):
        """`--verbose` swaps the live line for a no-op. A signature that did not match would make
        every download crash under the one flag people reach for when a download misbehaves."""
        monkeypatch.setattr(ui, "VERBOSE", True)
        with ui.progress("downloading") as update:
            update(0, 100)
            update(100, 100)
