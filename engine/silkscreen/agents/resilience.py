"""Provider failover.

The previous project advertised "multiple backups to ensure zero single points
of failure" and shipped fallbacks that had never been executed: one returned a
streaming iterator to a caller expecting a string, another read a response
field that did not exist. A fallback path that is never exercised is not a
backup, it is a second bug waiting for the first one to happen.

So two rules here. Every provider's output is validated before it is accepted,
because "it returned something" and "it returned usable text" are different
claims. And every fallback path has a test that forces the primary to fail.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .model import Document, Model, ModelError

__all__ = ["Attempt", "FallbackModel", "AllProvidersFailed", "Provider"]


class AllProvidersFailed(ModelError):
    """Every provider in the chain failed. Carries all of their errors."""

    def __init__(self, attempts: list[Attempt]):
        self.attempts = attempts
        detail = "; ".join(f"{a.provider}: {a.error}" for a in attempts if a.error)
        super().__init__(f"all {len(attempts)} attempts failed -- {detail}")


@dataclass(frozen=True)
class Provider:
    """A named model, plus how many times it is worth retrying."""

    name: str
    model: Model
    attempts: int = 2

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")


@dataclass(frozen=True)
class Attempt:
    """One call against one provider, successful or not."""

    provider: str
    ok: bool
    error: str | None = None
    elapsed_s: float = 0.0


def _validate(text: object, provider: str) -> str:
    """Accept only non-empty text.

    This is the check the old code skipped. A provider returning a generator, a
    response object, or an empty string is a failure -- surfacing it here sends
    the chain to the next provider instead of handing a caller something it
    cannot use.
    """
    if isinstance(text, str):
        if text.strip():
            return text
        raise ModelError(f"{provider} returned empty text")
    raise ModelError(
        f"{provider} returned {type(text).__name__}, expected str"
    )


@dataclass
class FallbackModel:
    """Tries each provider in order and returns the first usable response.

    Satisfies the :class:`~silkscreen.agents.model.Model` protocol, so it drops
    in anywhere a single model does.
    """

    providers: list[Provider]
    backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    #: Appended to on every call, successful or not. Read it to see which
    #: provider actually served a request.
    log: list[Attempt] = field(default_factory=list)
    _sleep: Callable[[float], None] = time.sleep
    #: Called immediately before every provider attempt. Services can use this
    #: to coordinate quota pacing across retries without changing providers.
    before_attempt: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("FallbackModel needs at least one provider")

    @property
    def last_provider(self) -> str | None:
        """Which provider served the most recent successful call."""
        for attempt in reversed(self.log):
            if attempt.ok:
                return attempt.provider
        return None

    @property
    def last_model(self) -> str | None:
        """Concrete model id behind :attr:`last_provider`, when it exposes one."""
        served = self.last_provider
        for provider in self.providers:
            if provider.name == served:
                value = getattr(provider.model, "model", None)
                return str(value) if value else None
        return None

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        attempts: list[Attempt] = []
        for provider in self.providers:
            for try_no in range(provider.attempts):
                if self.before_attempt is not None:
                    self.before_attempt(provider.name)
                started = time.monotonic()
                try:
                    raw = provider.model.generate(
                        prompt,
                        documents=documents,
                        system=system,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    text = _validate(raw, provider.name)
                except Exception as exc:
                    attempt = Attempt(
                        provider=provider.name,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_s=time.monotonic() - started,
                    )
                    attempts.append(attempt)
                    self.log.append(attempt)
                    if try_no + 1 < provider.attempts:
                        self._sleep(
                            min(self.backoff_s * (2**try_no), self.max_backoff_s)
                        )
                    continue

                attempt = Attempt(
                    provider=provider.name,
                    ok=True,
                    elapsed_s=time.monotonic() - started,
                )
                attempts.append(attempt)
                self.log.append(attempt)
                return text

        raise AllProvidersFailed(attempts)
