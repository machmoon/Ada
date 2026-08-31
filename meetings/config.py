"""Configuration, validated up front.

Every value is read once, here, and a missing one is an error at construction
rather than an ``AttributeError`` three layers into a request. The Slack bot
learned this the same way: a half-configured integration that starts and then
fails on the first real event is worse than one that refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = ["MeetConfig", "ConfigError", "MEET_SCOPES"]

#: OAuth scopes this integration needs. Read-only on purpose: nothing here
#: creates, modifies or joins a meeting, and asking for write access we do not
#: use is how an integration gets refused by an admin who reads the consent
#: screen.
MEET_SCOPES = (
    "https://www.googleapis.com/auth/meetings.space.readonly",
)

#: Meet REST API v2. Pinned rather than "latest": a silent major-version change
#: would alter the response shape under a parser that has no way to notice.
DEFAULT_API_BASE = "https://meet.googleapis.com/v2"


class ConfigError(RuntimeError):
    """The integration is not configured well enough to start."""


@dataclass(frozen=True)
class MeetConfig:
    """What the Meet front end needs to run.

    ``access_token`` is an OAuth 2.0 bearer token for a user or service account
    with :data:`MEET_SCOPES`. This package deliberately does **not** implement
    the OAuth dance: token acquisition, refresh and storage belong to the host
    application, and a package that mints its own credentials is a package that
    has to be trusted with them.
    """

    access_token: str
    api_base: str = DEFAULT_API_BASE
    #: Only act on transcripts from these meeting spaces. Empty means every
    #: conference the token can see, which is rarely what anyone wants.
    space_allowlist: frozenset[str] = field(default_factory=frozenset)
    #: Ignore conferences that ended longer ago than this. A first run against
    #: a busy account would otherwise replay months of meetings, each one a
    #: paid pipeline run.
    max_age_hours: float = 24.0
    #: Refuse to start more than this many board runs from one poll, for the
    #: same reason.
    max_runs_per_poll: int = 3

    def __post_init__(self) -> None:
        if not self.access_token or not self.access_token.strip():
            raise ConfigError(
                "no OAuth access token; set MEET_ACCESS_TOKEN or pass one in"
            )
        if not self.api_base.startswith("https://"):
            raise ConfigError(
                f"api_base must be https, got {self.api_base!r} -- a bearer "
                f"token must never travel over plaintext"
            )
        if self.max_runs_per_poll < 1:
            raise ConfigError("max_runs_per_poll must be at least 1")
        if self.max_age_hours <= 0:
            raise ConfigError("max_age_hours must be positive")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MeetConfig:
        """Build from the environment, naming what is missing."""
        env = dict(os.environ if env is None else env)
        token = env.get("MEET_ACCESS_TOKEN", "")
        if not token:
            raise ConfigError(
                "MEET_ACCESS_TOKEN is not set. This package does not perform "
                "the OAuth flow; obtain a token with the "
                f"{MEET_SCOPES[0]} scope and export it."
            )
        spaces = {
            s.strip() for s in env.get("MEET_SPACES", "").split(",") if s.strip()
        }
        return cls(
            access_token=token,
            api_base=env.get("MEET_API_BASE", DEFAULT_API_BASE),
            space_allowlist=frozenset(spaces),
            max_age_hours=_number(env, "MEET_MAX_AGE_HOURS", 24.0, float),
            max_runs_per_poll=_number(env, "MEET_MAX_RUNS_PER_POLL", 3, int),
        )

    def allows(self, space: str) -> bool:
        """Is this meeting space in scope?"""
        return not self.space_allowlist or space in self.space_allowlist


def _number(env: dict[str, str], key: str, default, cast):
    """Read a numeric setting, or raise :class:`ConfigError` naming it.

    A bare ``float("soon")`` escapes as ``ValueError``, which defeats this
    module's whole contract: every other bad value here surfaces as a
    ConfigError that says which variable is wrong. A typo in an env var should
    not reach the caller wearing a different exception type.
    """
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{key}={raw!r} is not a valid {cast.__name__}"
        ) from exc
