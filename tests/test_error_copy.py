"""Warning/error message quality (CLI + API).

A failure must say *what* broke and *how to fix it* — never leak a raw Python error, a bare
HTTP status, or an internal route path:

* the wizard's numeric prompts re-ask instead of crashing on a bad `int()` parse,
* `ApiError.user_message()` frames a 5xx and strips the route path, while a 4xx refusal and a
  transport failure reach the user verbatim (their detail is already the human reason).
"""

import pytest

from cuttle import api, ui


@pytest.fixture
def answers():
    def install(*ordered, **keyed):
        a = ui.Answers(*ordered, **keyed)
        ui.install_answers(a)
        return a
    yield install
    ui.install_answers(None)


class TestNumericPromptsReAsk:
    """Guards against a bad `int()` parse crashing the wizard with a raw `invalid literal` and
    no next step; `ask_int`/`ask_float` re-ask instead."""

    def test_a_non_number_is_re_asked_not_crashed(self, answers, capsys):
        a = answers("sixty", "60")           # a typo, then the correction
        assert ui.ask_int("Wall-clock limit? (minutes)") == 60
        assert len(a.asked) == 2             # it asked again rather than raising
        assert "whole number" in capsys.readouterr().err  # and said why

    def test_a_blank_is_re_asked(self, answers):
        answers("", "5")
        assert ui.ask_int("How many GPUs?") == 5

    def test_a_float_may_be_negative(self, answers):
        answers("-50")                        # a Karma removal
        assert ui.ask_float("How much Karma?") == -50.0


class TestApiErrorUserMessage:
    """What a command actually prints. `str(ApiError)` leads with the internal `/route` path and, for
    a 5xx, carries only a generic phrase — noise. `user_message()` is the presentable form."""

    def test_a_server_error_is_framed_not_leaked(self):
        e = api.ApiError("/submit", "Internal Server Error", status=500)
        msg = e.user_message()
        assert "/submit" not in msg               # no route path
        assert "Internal Server Error" not in msg  # no bare phrase
        assert "internal error" in msg and "try again" in msg

    def test_a_refusal_reaches_the_user_verbatim(self):
        # A 4xx detail is the Head's own human reason — a policy message, a bad name — and must land
        # intact, without the route path in front of it.
        e = api.ApiError("/passwd", "password must be at least 8 characters", status=422)
        assert e.user_message() == "password must be at least 8 characters"

    def test_a_transport_failure_reaches_the_user_verbatim(self):
        # status None = never got an answer (the "cannot reach the Head" message is already the fix).
        e = api.ApiError("/me", "cannot reach the Head at https://h — is it running?", status=None)
        assert e.user_message() == "cannot reach the Head at https://h — is it running?"


class TestATransportFailureNamesThePhaseItFailedIn:
    """The one message in this CLI with a diagnosis already attached to it.

    Every `httpx.RequestError` used to say "cannot reach the Head — is it running, and are you on the
    same LAN or the CCC mesh?". A 3GB outputs download that timed out mid-transfer said exactly that,
    sending its User to check a mesh that was working perfectly while the Head sat there holding their
    results. Never having reached the Head, and having reached it and lost it, are different faults
    with different next steps, and merging them costs a whole diagnosis.
    """

    def _client(self) -> api.Api:
        import httpx
        return api.Api(httpx.Client(base_url="http://head/api"), url="https://head")

    def test_a_connect_failure_asks_about_reachability(self):
        import httpx
        detail = self._client()._transport_detail(httpx.ConnectTimeout("timed out"))
        assert "cannot reach" in detail and "mesh" in detail

    def test_a_read_failure_says_it_got_there_and_lost_it(self):
        import httpx
        detail = self._client()._transport_detail(httpx.ReadTimeout("timed out"))
        assert "cannot reach" not in detail
        assert "reached the Head" in detail and "sent nothing" in detail

    def test_no_httpx_class_name_reaches_the_user(self):
        """A library's exception name is not a fix, in either branch."""
        import httpx
        for e in (httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.ReadError("x")):
            assert "httpx" not in self._client()._transport_detail(e).lower()
            assert type(e).__name__ not in self._client()._transport_detail(e)


class TestAStreamedDownloadIsBoundedPerChunkNotOverall:
    """The bug itself, as a constant. `timeout=30` as a bare float bounds the WHOLE request, so a
    transfer that takes four minutes to arrive is cancelled at thirty seconds however healthy it is.
    Split apart, `read` bounds the gap BETWEEN chunks — a limit a 3GB download can satisfy forever."""

    def test_the_read_budget_is_a_gap_not_a_deadline(self):
        assert api.TIMEOUT_STREAM.read is not None
        # No total/overall budget exists to cap a long transfer — httpx has no such field, and that
        # is the point: nothing here may expire because the download is simply big.
        assert not hasattr(api.TIMEOUT_STREAM, "total")

    def test_connecting_is_bounded_far_more_tightly_than_reading(self):
        """A Head that is off should say so in seconds; a Head that is working should be waited for."""
        assert api.TIMEOUT_STREAM.connect < api.TIMEOUT_STREAM.read
