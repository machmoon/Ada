"""Sending the run's results by Gmail, board file attached.

One endpoint: ``users/me/messages/send`` with a base64url-encoded RFC 2822
message in the ``raw`` field. The message is built with the stdlib ``email``
package -- multipart/mixed, a plain-text summary part, and the emitted
``.kicad_pcb`` as an application/octet-stream attachment -- so the tests can
decode ``raw`` back through the same stdlib and compare the attachment bytes
to what went in.

Gmail's hard cap is 25 MB for the whole encoded message; the guard here is
deliberately conservative (20 MB of attachment) because base64 inflates the
payload by a third and a clear local refusal beats a remote 413.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

from .auth import RERUN_HINT, AuthError
from .transport import GoogleError, HttpRequest, Transport, ensure_google_url

__all__ = ["MAX_ATTACHMENT_BYTES", "SEND_URL", "build_message", "send_run_email"]

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

#: Conservative bound under Gmail's 25 MB total-message cap.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def build_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    attachment_name: str,
    attachment: bytes,
) -> EmailMessage:
    """The MIME message, before encoding. ``From`` is left to Gmail, which
    stamps the authenticated user's own address and refuses forgeries."""
    if not to:
        raise GoogleError("bad_request", "an email needs at least one recipient")
    if len(attachment) > MAX_ATTACHMENT_BYTES:
        raise GoogleError(
            "attachment_too_large",
            f"{attachment_name} is {len(attachment) / 1_048_576:.1f} MB; Gmail "
            f"caps messages at 25 MB and this integration refuses attachments "
            f"over {MAX_ATTACHMENT_BYTES // 1_048_576} MB",
        )
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="octet-stream",
        filename=attachment_name,
    )
    return message


def send_run_email(
    token: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    board_path: Path,
    transport: Transport,
) -> str:
    """Send the summary with the board attached; returns Gmail's message id."""
    message = build_message(
        to=to,
        subject=subject,
        body=body,
        attachment_name=board_path.name,
        attachment=board_path.read_bytes(),
    )
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    response = transport(
        HttpRequest(
            "POST",
            ensure_google_url(SEND_URL),
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json.dumps({"raw": raw}).encode("utf-8"),
        )
    )
    if response.status == 401:
        raise AuthError(f"Gmail rejected the access token; {RERUN_HINT}")
    payload = response.json()
    if response.status >= 300:
        detail = str((payload.get("error") or {}).get("message") or "send failed")
        raise GoogleError(f"http_{response.status}", detail)
    return str(payload.get("id", ""))
