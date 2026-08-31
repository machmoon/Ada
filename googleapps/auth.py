"""OAuth 2.0 installed-app flow with PKCE, on the stdlib.

``python -m googleapps auth`` opens Google's consent page in the browser,
catches the redirect on a 127.0.0.1 loopback port, exchanges the code, and
writes the token JSON to the token path with mode 0o600. After that, every
Gmail and Calendar call goes through :func:`access_token`, which refreshes
transparently when the stored token has expired and persists what came back.

Security rules, all load-bearing:

- PKCE is S256. The ``code_verifier`` lives only in this process's memory for
  the duration of the flow -- it is never written to disk and never logged.
- The token file is chmod 0o600, on create *and* on every rewrite.
- Tokens are never printed. Errors that mention the token talk about the
  file, not the contents.
- A missing or revoked token raises :class:`AuthError` telling the user the
  one command that fixes it, rather than a bare HTTP 401.

The consent URL is on ``accounts.google.com``, which is deliberately absent
from the transport allowlist: it is only ever *opened in the user's browser*,
never addressed by this package's own HTTP client. The one token endpoint we
POST to is ``oauth2.googleapis.com``.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .transport import GoogleError, HttpRequest, Transport, ensure_google_url

__all__ = [
    "AUTH_URL",
    "TOKEN_URL",
    "SCOPES",
    "AuthError",
    "access_token",
    "build_auth_url",
    "exchange_code",
    "load_token",
    "parse_redirect",
    "pkce_pair",
    "run_auth_flow",
    "save_token",
    "token_file_is_private",
    "token_status",
]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Exactly what the two features need, nothing broader: send mail as the
#: user, and manage events. Neither scope can read the user's mailbox.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)

#: A token this close to expiry is treated as expired, so a request cannot
#: start with a token that dies mid-flight.
EXPIRY_SKEW_S = 60

RERUN_HINT = "run `python -m googleapps auth` to sign in again"


class AuthError(RuntimeError):
    """No usable token. The message always names the command that fixes it."""


# -- PKCE and URLs ---------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair, S256.

    The verifier is 64 random bytes base64url-encoded -- 86 characters,
    inside RFC 7636's 43..128 -- and must never be persisted anywhere.
    """
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_auth_url(
    client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            # A refresh token only arrives with offline access, and only
            # reliably on a consented prompt.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_URL}?{query}"


def parse_redirect(path: str, expected_state: str) -> str:
    """The authorization code out of the loopback redirect, or a refusal.

    Every failure mode raises rather than returning an empty code: a state
    mismatch is a CSRF attempt or a stale tab, and an ``error`` parameter is
    the user declining consent -- both must end the flow, not continue it.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    if "error" in query:
        raise AuthError(f"Google refused authorization: {query['error'][0]}")
    state = (query.get("state") or [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise AuthError("state mismatch on the OAuth redirect; aborting the flow")
    code = (query.get("code") or [""])[0]
    if not code:
        raise AuthError("the OAuth redirect carried no authorization code")
    return code


# -- token persistence -----------------------------------------------------


def save_token(path: Path, token: dict[str, Any]) -> None:
    """Write the token JSON with owner-only permissions.

    The mode is set at create time via ``os.open`` and re-asserted with
    ``chmod`` for the rewrite case, where ``O_CREAT``'s mode does not apply.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(token, handle, indent=2)
    os.chmod(path, 0o600)


def load_token(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AuthError(f"no token at {path}; {RERUN_HINT}")
    try:
        token = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"unreadable token at {path}; {RERUN_HINT}") from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        raise AuthError(f"malformed token at {path}; {RERUN_HINT}")
    return token


def token_status(path: Path, now: float | None = None) -> str:
    """``missing`` / ``expired`` / ``valid`` -- purely local, for ``check``."""
    try:
        token = load_token(path)
    except AuthError:
        return "missing"
    current = time.time() if now is None else now
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        return "expired (will refresh on next use)"
    return "valid"


def _stamp(token: dict[str, Any], now: float) -> dict[str, Any]:
    """Convert ``expires_in`` (relative, from Google) to ``expires_at``
    (absolute, ours), which is the only form a later process can act on."""
    stamped = dict(token)
    stamped["expires_at"] = now + float(stamped.pop("expires_in", 0))
    return stamped


# -- token endpoint --------------------------------------------------------


def _token_post(transport: Transport, form: dict[str, str]) -> dict[str, Any]:
    request = HttpRequest(
        "POST",
        ensure_google_url(TOKEN_URL),
        {"Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode(form).encode("ascii"),
    )
    response = transport(request)
    payload = response.json()
    if response.status >= 300 or "error" in payload:
        code = str(payload.get("error") or f"http_{response.status}")
        if code == "invalid_grant":
            # The refresh token was revoked or has expired; only a new
            # consent can mint another one.
            raise AuthError(f"the stored Google token was revoked; {RERUN_HINT}")
        raise GoogleError(code, str(payload.get("error_description") or "token call"))
    return payload


def exchange_code(
    transport: Transport,
    config: Config,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Trade the authorization code for tokens and persist them."""
    payload = _token_post(
        transport,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    token = _stamp(payload, time.time() if now is None else now)
    save_token(config.token_path, token)
    return token


def _refresh(
    transport: Transport, config: Config, token: dict[str, Any], now: float
) -> dict[str, Any]:
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        raise AuthError(f"the stored token cannot be refreshed; {RERUN_HINT}")
    payload = _token_post(
        transport,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        },
    )
    # Google does not resend the refresh token on a refresh; keep ours.
    merged = {**token, **_stamp(payload, now)}
    merged.setdefault("refresh_token", refresh)
    save_token(config.token_path, merged)
    return merged


def access_token(
    config: Config, transport: Transport, *, now: float | None = None
) -> str:
    """A currently-valid access token, refreshing and persisting if needed."""
    current = time.time() if now is None else now
    token = load_token(config.token_path)
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        config.require_oauth()
        token = _refresh(transport, config, token, current)
    return str(token["access_token"])


# -- the interactive flow --------------------------------------------------

_LANDING = (
    b"<!doctype html><meta charset='utf-8'><title>silkscreen</title>"
    b"<p>Signed in. You can close this tab and return to the terminal.</p>"
)


def _loopback_authorize(
    open_browser: Callable[[str], Any] = webbrowser.open,
    timeout_s: float = 300.0,
) -> Callable[[str, str], str]:
    """The real user-facing half: browser out, loopback redirect back in.

    Returns an ``authorize(auth_url_template, state) -> redirect path``
    callable. Split out so :func:`run_auth_flow` can be driven end to end by
    tests with a fake authorizer -- everything except the browser and the
    socket is then the code that runs in production.
    """

    def authorize(build_url: Callable[[str], str], state: str) -> str:
        captured: dict[str, str] = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                captured["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_LANDING)
                done.set()

            def log_message(self, *args: Any) -> None:
                """Silence the default stderr access log; the redirect URL
                carries the authorization code and must not be printed."""

        with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 1.0
            port = server.server_address[1]
            url = build_url(f"http://127.0.0.1:{port}")
            print("Opening the Google consent page in your browser…")
            print("If nothing opens, paste this URL yourself:")
            print(f"  {url}")
            open_browser(url)
            deadline = time.monotonic() + timeout_s
            while not done.is_set():
                if time.monotonic() > deadline:
                    raise AuthError("timed out waiting for the OAuth redirect")
                server.handle_request()
        return captured.get("path", "")

    return authorize


def run_auth_flow(
    config: Config,
    transport: Transport,
    *,
    authorize: Callable[[Callable[[str], str], str], str] | None = None,
    now: float | None = None,
) -> Path:
    """The whole ``auth`` subcommand: consent, redirect, exchange, persist.

    Returns the token path. The verifier and the state exist only inside this
    call frame.
    """
    config.require_oauth()
    verifier, challenge = pkce_pair()
    state = _b64url(secrets.token_bytes(32))
    redirect: dict[str, str] = {}

    def build_url(redirect_uri: str) -> str:
        redirect["uri"] = redirect_uri
        return build_auth_url(config.client_id, redirect_uri, challenge, state)

    path = (authorize or _loopback_authorize())(build_url, state)
    code = parse_redirect(path, state)
    exchange_code(
        transport,
        config,
        code=code,
        verifier=verifier,
        redirect_uri=redirect["uri"],
        now=now,
    )
    return config.token_path


def token_file_is_private(path: Path) -> bool:
    """True when the token file exists and only its owner can read it."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode == 0o600
