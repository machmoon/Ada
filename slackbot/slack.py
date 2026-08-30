"""A small Slack Web API client, and request-signature verification.

Stdlib only, for the same reason ``service/app.py`` is: this repository's
dependency list is short and honest, and the three Slack endpoints a bot needs
are three HTTP calls. ``slack_sdk`` would be a reasonable dependency; it is not
a necessary one.

Every network call goes through :class:`Transport`, so the tests exercise the
real request construction -- URL, headers, encoding, the two-step file upload --
against a recorded transport instead of the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "SlackError",
    "SlackClient",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "urllib_transport",
    "verify_signature",
    "SIGNATURE_VERSION",
    "MAX_SIGNATURE_AGE_S",
]

API_ROOT = "https://slack.com/api/"
SIGNATURE_VERSION = "v0"
#: Slack's own recommendation. An older timestamp is a replay, not a slow
#: network, and is refused even when the signature itself is valid.
MAX_SIGNATURE_AGE_S = 60 * 5


class SlackError(RuntimeError):
    """Slack answered, and the answer was a failure.

    ``code`` is Slack's own ``error`` string (``channel_not_found``,
    ``not_in_channel``, ``invalid_auth``), which is what tells an operator
    whether the fix is an invite, a scope, or a new token.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


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
            raise SlackError(
                "bad_response", f"HTTP {self.status} was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SlackError("bad_response", "expected a JSON object")
        return payload


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def urllib_transport(timeout: float = 30.0) -> Transport:
    """The real transport. An HTTP error is a response, not an exception --
    Slack puts its own error string in the body of a 200 *and* of a 429."""

    def send(request: HttpRequest) -> HttpResponse:
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
            raise SlackError("network_error", str(exc.reason)) from exc

    return send


def verify_signature(
    signing_secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    """True if this request really came from Slack, recently.

    Both halves matter. The HMAC proves the body was not tampered with; the age
    check is what stops a valid, captured request from being replayed at us
    later. Comparison is constant-time, and every failure mode -- missing
    header, unparseable timestamp, wrong digest -- returns False rather than
    raising, so the caller has exactly one branch to write.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        sent_at = float(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_SIGNATURE_AGE_S:
        return False

    base = b"%s:%s:%s" % (SIGNATURE_VERSION.encode(), timestamp.encode(), body)
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"{SIGNATURE_VERSION}={digest}"
    return hmac.compare_digest(expected, signature)


class SlackClient:
    """The handful of Web API methods this bot uses."""

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
    ):
        self._token = token
        self._transport = transport or urllib_transport()
        self._sleep = sleep
        self._max_retries = max_retries

    # -- plumbing ---------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON API method, retrying only what is worth retrying."""
        body = json.dumps(
            {k: v for k, v in payload.items() if v is not None}
        ).encode("utf-8")
        request = HttpRequest(
            "POST",
            API_ROOT + method,
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body,
        )
        return self._send_api(request, method)

    def _send_api(self, request: HttpRequest, method: str) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            response = self._transport(request)
            if response.status == 429 and attempt < self._max_retries:
                self._sleep(self._retry_after(response))
                continue
            data = response.json()
            if data.get("ok"):
                return data
            code = str(data.get("error") or f"http_{response.status}")
            if code == "ratelimited" and attempt < self._max_retries:
                self._sleep(self._retry_after(response))
                continue
            raise SlackError(code, f"{method} failed")
        raise SlackError("ratelimited", f"{method} gave up after retries")

    @staticmethod
    def _retry_after(response: HttpResponse) -> float:
        # The header is authoritative when present; a second is a safe floor
        # when it is not, since the alternative is a hot loop.
        try:
            return max(1.0, float(response.json().get("retry_after", 1)))
        except SlackError:
            return 1.0

    # -- messages ---------------------------------------------------------

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """Post a message; returns its ``ts``, which is a thread handle.

        ``text`` is always sent even when ``blocks`` are, because it is what
        notifications and screen readers use.
        """
        data = self._call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        return str(data.get("ts", ""))

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Edit a message in place -- how a progress line advances without
        turning a thread into a scrollback of near-identical posts."""
        self._call(
            "chat.update",
            {"channel": channel, "ts": ts, "text": text, "blocks": blocks},
        )

    def add_reaction(self, channel: str, ts: str, name: str) -> None:
        """React to a message. A failure here is cosmetic, so it is swallowed:
        losing an emoji must never lose a run."""
        try:
            self._call(
                "reactions.add", {"channel": channel, "timestamp": ts, "name": name}
            )
        except SlackError:
            pass

    def remove_reaction(self, channel: str, ts: str, name: str) -> None:
        try:
            self._call(
                "reactions.remove", {"channel": channel, "timestamp": ts, "name": name}
            )
        except SlackError:
            pass

    # -- files ------------------------------------------------------------

    def upload_file(
        self,
        channel: str,
        filename: str,
        content: bytes,
        *,
        thread_ts: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
    ) -> str:
        """Upload one file into a channel (and thread), returning its file id.

        This is Slack's three-step external upload: ask for a URL, PUT the
        bytes at it, then tell Slack where the finished file belongs. The old
        one-shot ``files.upload`` is retired, so the extra hops are not
        optional.
        """
        if not content:
            raise SlackError("empty_file", f"{filename} had no content")

        query = urllib.parse.urlencode(
            {"filename": filename, "length": str(len(content))}
        )
        reserved = self._send_api(
            HttpRequest(
                "GET",
                f"{API_ROOT}files.getUploadURLExternal?{query}",
                {"Authorization": f"Bearer {self._token}"},
            ),
            "files.getUploadURLExternal",
        )
        upload_url = str(reserved.get("upload_url", ""))
        file_id = str(reserved.get("file_id", ""))
        if not upload_url or not file_id:
            raise SlackError("bad_response", "no upload URL was issued")

        boundary = f"----silkscreen{uuid.uuid4().hex}"
        body = _multipart(boundary, filename, content)
        posted = self._transport(
            HttpRequest(
                "POST",
                upload_url,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                body,
            )
        )
        if posted.status >= 300:
            raise SlackError("upload_failed", f"HTTP {posted.status} storing bytes")

        self._call(
            "files.completeUploadExternal",
            {
                "files": [{"id": file_id, "title": title or filename}],
                "channel_id": channel,
                "thread_ts": thread_ts,
                "initial_comment": initial_comment,
            },
        )
        return file_id


def _multipart(boundary: str, filename: str, content: bytes) -> bytes:
    """One-part ``multipart/form-data`` body, built by hand.

    ``filename`` is quoted after stripping quotes and newlines from it: a
    header injected through a filename would be a real hole, and the name of a
    board file is never something a user needs those characters in.
    """
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    return head + content + f"\r\n--{boundary}--\r\n".encode()
