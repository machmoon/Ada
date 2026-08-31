"""Gmail: the MIME message survives its own encoding, and the guards hold."""

from __future__ import annotations

import base64
import email
import email.policy
import json

import pytest

from googleapps import gmail
from googleapps.auth import AuthError
from googleapps.tests.fakes import RecordingTransport
from googleapps.transport import GoogleError, HttpResponse

BOARD_BYTES = b"(kicad_pcb (version 20240108) (generator silkscreen))\n"


def board_file(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(BOARD_BYTES)
    return path


def sent_message(transport):
    """Decode what reached the API back into a parsed MIME message."""
    body = json.loads(transport.requests[-1].body)
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_the_send_hits_the_documented_endpoint_with_a_bearer_token(tmp_path):
    transport = RecordingTransport()
    message_id = gmail.send_run_email(
        "ya29.tok",
        to=["team@example.com"],
        subject="silkscreen: board ready",
        body="2 parts",
        board_path=board_file(tmp_path),
        transport=transport,
    )
    assert message_id == "msg-1"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    )
    assert request.headers["Authorization"] == "Bearer ya29.tok"


def test_the_raw_field_round_trips_through_base64url(tmp_path):
    """The repo's round-trip discipline, applied to MIME: the attachment
    bytes recovered from the ``raw`` field equal the file that went in."""
    transport = RecordingTransport()
    gmail.send_run_email(
        "t",
        to=["a@example.com", "b@example.com"],
        subject="the subject line",
        body="the summary",
        board_path=board_file(tmp_path),
        transport=transport,
    )
    message = sent_message(transport)
    assert message["To"] == "a@example.com, b@example.com"
    assert message["Subject"] == "the subject line"
    parts = list(message.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "board.kicad_pcb"
    assert parts[0].get_content() == BOARD_BYTES
    assert message.get_body(("plain",)).get_content().strip() == "the summary"


def test_the_raw_field_is_base64url_not_plain_base64(tmp_path):
    # 0xfb 0xff forces '-' and '_' into the url-safe alphabet ('+' and '/'
    # in plain base64), so a wrong encoder cannot pass this.
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(b"\xfb\xff" * 12)
    transport = RecordingTransport()
    gmail.send_run_email(
        "t", to=["a@example.com"], subject="s", body="b",
        board_path=path, transport=transport,
    )
    raw = json.loads(transport.requests[0].body)["raw"]
    assert "+" not in raw and "/" not in raw


def test_an_oversized_attachment_is_refused_locally(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(b"x" * (gmail.MAX_ATTACHMENT_BYTES + 1))
    transport = RecordingTransport()
    with pytest.raises(GoogleError, match="attachment_too_large"):
        gmail.send_run_email(
            "t", to=["a@example.com"], subject="s", body="b",
            board_path=path, transport=transport,
        )
    assert transport.requests == []  # refused before any bytes left


def test_no_recipients_is_an_error(tmp_path):
    with pytest.raises(GoogleError, match="recipient"):
        gmail.send_run_email(
            "t", to=[], subject="s", body="b",
            board_path=board_file(tmp_path), transport=RecordingTransport(),
        )


def test_a_401_tells_the_user_to_reauthenticate(tmp_path):
    transport = RecordingTransport(
        {"gmail.googleapis.com": HttpResponse(401, b"{}")}
    )
    with pytest.raises(AuthError, match="python -m googleapps auth"):
        gmail.send_run_email(
            "t", to=["a@example.com"], subject="s", body="b",
            board_path=board_file(tmp_path), transport=transport,
        )
