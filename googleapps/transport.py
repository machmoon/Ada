"""One HTTP seam for every Google call, with a hard host allowlist.

Stdlib only, for the same reason ``service/app.py`` is: the handful of REST
calls this package makes do not justify a client library. Every request goes
through :class:`Transport`, an injectable callable, so the tests exercise the
real request construction -- URL, method, headers, body encoding -- against a
recorded transport instead of the network.

The allowlist is enforced at request-construction time, not inside the real
transport, so it holds for *every* transport including the fakes: code that
builds a request for a non-Google host is wrong whether or not that request
would have left the machine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ALLOWED_HOSTS",
    "GoogleError",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "ensure_google_url",
    "mask",
    "urllib_transport",
]

#: The only hosts this package will ever address. Exact matches -- a suffix
#: check would wave through ``chat.googleapis.com.evil.example``.
ALLOWED_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "gmail.googleapis.com",
        "www.googleapis.com",
        "chat.googleapis.com",
    }
)


class GoogleError(RuntimeError):
    """A Google call failed, or was refused before it was made.

    ``code`` is Google's own error status when one came back (``401``,
    ``invalid_grant``), or a local reason (``bad_host``) when the request was
    never sent.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def mask(secret: str, keep: int = 4) -> str:
    """A printable stand-in for a secret: its tail, never its value.

    Used for every log or terminal line that has to prove a setting is
    present. Short values show nothing at all rather than most of themselves.
    """
    secret = secret or ""
    if len(secret) <= keep * 2:
        return "<set>" if secret else "<unset>"
    return f"…{secret[-keep:]}"


def ensure_google_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to an allowlisted Google host.

    Raises:
        GoogleError: for any other scheme or host. The refusal happens before
            a transport sees the request, so no byte -- and no bearer token --
            can leave toward a host this package does not know.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise GoogleError(
            "bad_host", f"refusing non-https URL scheme {parsed.scheme!r}"
        )
    if parsed.hostname not in ALLOWED_HOSTS:
        raise GoogleError(
            "bad_host",
            f"refusing to send to {parsed.hostname!r}: not a known Google API host",
        )
    return url


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleError(
                "bad_response", f"HTTP {self.status} was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleError("bad_response", "expected a JSON object")
        return payload


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def urllib_transport(timeout: float = 30.0) -> Transport:
    """The real transport. An HTTP error status is a response, not an
    exception -- Google puts the useful error JSON in the body of a 4xx."""

    def send(request: HttpRequest) -> HttpResponse:
        ensure_google_url(request.url)
        req = urllib.request.Request(
            request.url,
            data=request.body or None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except urllib.error.URLError as exc:
            raise GoogleError("network_error", str(exc.reason)) from exc

    return send
