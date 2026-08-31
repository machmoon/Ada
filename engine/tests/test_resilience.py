"""Failover: every backup path is executed here, not assumed.

The previous project's fallbacks had never run. These tests exist so that
cannot be true again -- each one forces the primary to fail in a different way.
"""

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel
from silkscreen.agents.resilience import (
    AllProvidersFailed,
    FallbackModel,
    Provider,
)


class Boom:
    """Raises. The ordinary failure."""

    def __init__(self, exc=None):
        self.exc = exc or ModelError("upstream 503")
        self.calls = 0

    def generate(self, prompt, **kw):
        self.calls += 1
        raise self.exc


class ReturnsIterator:
    """Returns a streaming iterator to a caller expecting a string.

    This is the exact bug that shipped last time: the fallback 'worked' and
    handed back an object nobody could use.
    """

    def generate(self, prompt, **kw):
        return iter(["chunk one", "chunk two"])


class ReturnsEmpty:
    def generate(self, prompt, **kw):
        return "   \n  "


class ReturnsNone:
    def generate(self, prompt, **kw):
        return None


def chain(*providers, **kw):
    kw.setdefault("_sleep", lambda _s: None)
    return FallbackModel(providers=list(providers), **kw)


def test_primary_serves_when_healthy():
    good = ScriptedModel(responses=["ok"])
    backup = Boom()
    fb = chain(Provider("primary", good), Provider("backup", backup))
    assert fb.generate("hi") == "ok"
    assert fb.last_provider == "primary"
    assert backup.calls == 0, "backup must not be called when primary works"


def test_falls_through_to_the_backup_when_the_primary_raises():
    fb = chain(
        Provider("primary", Boom(), attempts=1),
        Provider("backup", ScriptedModel(responses=["from backup"])),
    )
    assert fb.generate("hi") == "from backup"
    assert fb.last_provider == "backup"


def test_a_provider_returning_an_iterator_is_a_failure_not_a_success():
    fb = chain(
        Provider("streamer", ReturnsIterator(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert fb.last_provider == "backup"
    assert "expected str" in fb.log[0].error


def test_empty_text_is_a_failure():
    fb = chain(
        Provider("blank", ReturnsEmpty(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert "empty text" in fb.log[0].error


def test_none_is_a_failure():
    fb = chain(
        Provider("none", ReturnsNone(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert "NoneType" in fb.log[0].error


def test_retries_within_a_provider_before_moving_on():
    flaky = Boom()
    fb = chain(
        Provider("flaky", flaky, attempts=3),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert flaky.calls == 3, "should exhaust its retries before failing over"


def test_every_provider_attempt_passes_through_the_pre_call_hook():
    called = []
    fb = chain(
        Provider("primary", Boom(), attempts=2),
        Provider("backup", ScriptedModel(responses=["ok"]), attempts=1),
        before_attempt=called.append,
    )

    assert fb.generate("hi") == "ok"
    assert called == ["primary", "primary", "backup"]


def test_backoff_grows_and_is_capped():
    delays = []
    fb = FallbackModel(
        providers=[Provider("flaky", Boom(), attempts=5)],
        backoff_s=1.0,
        max_backoff_s=4.0,
        _sleep=delays.append,
    )
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert delays == [1.0, 2.0, 4.0, 4.0], "exponential, then capped"


def test_all_failing_raises_with_every_error_attached():
    fb = chain(
        Provider("a", Boom(ModelError("a down")), attempts=1),
        Provider("b", Boom(ModelError("b down")), attempts=1),
    )
    with pytest.raises(AllProvidersFailed) as exc:
        fb.generate("hi")
    assert len(exc.value.attempts) == 2
    assert "a down" in str(exc.value) and "b down" in str(exc.value)


def test_third_provider_is_reached():
    fb = chain(
        Provider("a", Boom(), attempts=1),
        Provider("b", ReturnsIterator(), attempts=1),
        Provider("c", ScriptedModel(responses=["third"])),
    )
    assert fb.generate("hi") == "third"
    assert [a.provider for a in fb.log] == ["a", "b", "c"]


def test_an_unexpected_exception_type_still_fails_over():
    class Weird:
        def generate(self, prompt, **kw):
            raise KeyError("candidates")  # the old parse-a-missing-field bug

    fb = chain(
        Provider("weird", Weird(), attempts=1),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert "KeyError" in fb.log[0].error


def test_arguments_reach_the_provider_that_serves():
    scripted = ScriptedModel(responses=["ok"])
    fb = chain(Provider("primary", scripted))
    fb.generate("the prompt", system="be terse", temperature=0.7)
    assert scripted.calls[0]["prompt"] == "the prompt"
    assert scripted.calls[0]["system"] == "be terse"


def test_an_empty_chain_is_rejected_at_construction():
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackModel(providers=[])


def test_attempts_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        Provider("x", ScriptedModel(), attempts=0)


def test_last_provider_is_none_before_any_success():
    fb = chain(Provider("a", Boom(), attempts=1))
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert fb.last_provider is None
