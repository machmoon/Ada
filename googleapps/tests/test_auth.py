"""The OAuth flow: PKCE, the exchange, refresh, and the token file's mode.

The PKCE assertions recompute the S256 challenge with independent hashlib
calls rather than through the code under test -- the same discipline the
placement tests use for geometry: a check written in terms of the code it
checks shares its blind spots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import urllib.parse

import pytest

from googleapps import auth
from googleapps.config import Config
from googleapps.tests.fakes import RecordingTransport, config_with_token, valid_token
from googleapps.transport import HttpResponse

NOW = 1_756_600_000.0


def oauth_config(tmp_path) -> Config:
    return Config(
        client_id="cid.apps.googleusercontent.com",
        client_secret="csecret",
        token_path=tmp_path / "token.json",
    )


def form(request) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(request.body.decode()).items()}


# -- PKCE ------------------------------------------------------------------


def test_the_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = auth.pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    assert 43 <= len(verifier) <= 128  # RFC 7636's bounds


def test_every_flow_gets_a_fresh_pair():
    assert auth.pkce_pair() != auth.pkce_pair()


def test_the_auth_url_carries_the_challenge_and_both_scopes():
    url = auth.build_auth_url("cid", "http://127.0.0.1:9/", "CHAL", "STATE")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlsplit(url).query).items()}
    assert query["code_challenge"] == "CHAL"
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline"
    assert "gmail.send" in query["scope"]
    assert "calendar.events" in query["scope"]


# -- the redirect ----------------------------------------------------------


def test_the_redirect_yields_its_code():
    assert auth.parse_redirect("/?state=S&code=abc", "S") == "abc"


@pytest.mark.parametrize(
    "path",
    [
        "/?state=WRONG&code=abc",  # CSRF or a stale tab
        "/?code=abc",  # no state at all
        "/?state=S&error=access_denied",  # the user declined
        "/?state=S",  # no code
    ],
)
def test_bad_redirects_are_refused(path):
    with pytest.raises(auth.AuthError):
        auth.parse_redirect(path, "S")


# -- the exchange ----------------------------------------------------------


def test_the_exchange_posts_the_verifier_to_the_token_endpoint(tmp_path):
    transport = RecordingTransport(
        {"oauth2.googleapis.com/token": {
            "access_token": "ya29.first",
            "refresh_token": "1//r",
            "expires_in": 3599,
        }}
    )
    config = oauth_config(tmp_path)
    auth.exchange_code(
        transport, config, code="the-code", verifier="the-verifier",
        redirect_uri="http://127.0.0.1:41321/", now=NOW,
    )
    request = transport.requests[0]
    assert request.url == "https://oauth2.googleapis.com/token"
    assert request.method == "POST"
    body = form(request)
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "the-code"
    assert body["code_verifier"] == "the-verifier"
    assert body["redirect_uri"] == "http://127.0.0.1:41321/"


def test_the_stored_token_is_mode_0600_and_never_holds_the_verifier(tmp_path):
    transport = RecordingTransport()
    config = oauth_config(tmp_path)
    auth.exchange_code(
        transport, config, code="c", verifier="super-secret-verifier",
        redirect_uri="http://127.0.0.1:1/", now=NOW,
    )
    mode = stat.S_IMODE(config.token_path.stat().st_mode)
    assert mode == 0o600
    stored = config.token_path.read_text()
    assert "super-secret-verifier" not in stored
    # Relative expiry became absolute, so a later process can act on it.
    assert json.loads(stored)["expires_at"] == pytest.approx(NOW + 3599)


def test_a_rewrite_reasserts_the_mode(tmp_path):
    path = tmp_path / "token.json"
    auth.save_token(path, {"access_token": "a"})
    path.chmod(0o644)
    auth.save_token(path, {"access_token": "b"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- the whole flow, wired together ----------------------------------------


def test_run_auth_flow_end_to_end_with_a_fake_browser(tmp_path):
    """Everything but the socket and the browser: the URL the user would
    visit carries the S256 of exactly the verifier the exchange later sends."""
    transport = RecordingTransport()
    config = oauth_config(tmp_path)
    seen: dict[str, str] = {}

    def authorize(build_url, state):
        url = build_url("http://127.0.0.1:55555")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlsplit(url).query).items()}
        seen["challenge"] = query["code_challenge"]
        return f"/?state={urllib.parse.quote(state)}&code=granted-code"

    path = auth.run_auth_flow(config, transport, authorize=authorize, now=NOW)
    assert path == config.token_path
    body = form(transport.requests[0])
    recomputed = (
        base64.urlsafe_b64encode(
            hashlib.sha256(body["code_verifier"].encode()).digest()
        ).decode().rstrip("=")
    )
    assert recomputed == seen["challenge"]
    assert body["code"] == "granted-code"
    assert config.token_path.exists()


# -- refresh ---------------------------------------------------------------


def test_a_valid_token_is_used_without_any_network_call(tmp_path):
    transport = RecordingTransport()
    config = config_with_token(tmp_path, expires_at=NOW + 3000)
    token = auth.access_token(config, transport, now=NOW)
    assert token == "ya29.live-token"
    assert transport.requests == []


def test_an_expired_token_refreshes_and_persists(tmp_path):
    transport = RecordingTransport()
    config = config_with_token(tmp_path, expires_at=NOW - 10)
    token = auth.access_token(config, transport, now=NOW)
    assert token == "ya29.refreshed"
    body = form(transport.requests[0])
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "1//refresh-token"
    stored = json.loads(config.token_path.read_text())
    assert stored["access_token"] == "ya29.refreshed"
    # Google does not resend the refresh token; ours must survive the merge.
    assert stored["refresh_token"] == "1//refresh-token"
    assert stat.S_IMODE(config.token_path.stat().st_mode) == 0o600


def test_a_missing_token_names_the_command_that_fixes_it(tmp_path):
    config = oauth_config(tmp_path)
    with pytest.raises(auth.AuthError, match="python -m googleapps auth"):
        auth.access_token(config, RecordingTransport(), now=NOW)


def test_a_revoked_refresh_token_names_the_command_too(tmp_path):
    transport = RecordingTransport(
        {"oauth2.googleapis.com/token": HttpResponse(
            400, b'{"error": "invalid_grant"}')}
    )
    config = config_with_token(tmp_path, expires_at=NOW - 10)
    with pytest.raises(auth.AuthError, match="python -m googleapps auth"):
        auth.access_token(config, transport, now=NOW)


def test_token_status_is_purely_local(tmp_path):
    config = config_with_token(tmp_path)
    assert auth.token_status(config.token_path, now=NOW) == "valid"
    auth.save_token(config.token_path, valid_token(expires_at=NOW - 1))
    assert "expired" in auth.token_status(config.token_path, now=NOW)
    assert auth.token_status(tmp_path / "nope.json", now=NOW) == "missing"
