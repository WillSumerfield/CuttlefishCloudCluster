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
