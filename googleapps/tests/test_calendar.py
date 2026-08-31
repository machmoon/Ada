"""Calendar: the insert that mints a Meet link, and what comes back."""

from __future__ import annotations

import json

import pytest

from googleapps import calendar
from googleapps.auth import AuthError
from googleapps.tests.fakes import RecordingTransport
from googleapps.transport import HttpResponse

EVENT_RESPONSE = {
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "conferenceData": {
        "entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
            {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg"},
        ]
    },
}

NOW = 1_756_600_000.0  # 2025-08-31 00:26:40 UTC


def schedule(transport, **kwargs):
    return calendar.schedule_review(
        "ya29.tok",
        board_name="ldo-board",
        blocker_titles=["VIN has no bulk capacitor"],
        attendees=["lead@example.com", "james@example.com"],
        transport=transport,
        now=NOW,
        request_id="fixed-request-id",
        **kwargs,
    )


def test_the_insert_asks_google_to_mint_a_meet_link():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    event = schedule(transport)
    request = transport.requests[0]
    assert request.method == "POST"
    # conferenceDataVersion=1 is what allows createRequest at all.
    assert request.url == (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        "?conferenceDataVersion=1"
    )
    assert request.headers["Authorization"] == "Bearer ya29.tok"
    body = json.loads(request.body)
    create = body["conferenceData"]["createRequest"]
    assert create["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert create["requestId"] == "fixed-request-id"
    assert event.html_link == EVENT_RESPONSE["htmlLink"]
    assert event.meet_uri == "https://meet.google.com/xyz-abcd-efg"


def test_the_event_is_titled_after_the_board_and_carries_the_blockers():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    schedule(transport)
    body = json.loads(transport.requests[0].body)
    assert body["summary"] == "Design review: ldo-board"
    assert "VIN has no bulk capacitor" in body["description"]
    assert body["attendees"] == [
        {"email": "lead@example.com"},
        {"email": "james@example.com"},
    ]


def test_the_times_are_rfc3339_a_day_out_for_half_an_hour():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    schedule(transport)
    body = json.loads(transport.requests[0].body)
    assert body["start"] == {"dateTime": "2025-09-01T00:26:40Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2025-09-01T00:56:40Z", "timeZone": "UTC"}


def test_hangout_link_is_the_fallback_when_entry_points_are_absent():
    transport = RecordingTransport(
        {"calendars/primary/events": {
            "htmlLink": "L", "hangoutLink": "https://meet.google.com/fallback"}}
    )
    assert schedule(transport).meet_uri == "https://meet.google.com/fallback"


def test_a_401_tells_the_user_to_reauthenticate():
    transport = RecordingTransport(
        {"calendars/primary/events": HttpResponse(401, b"{}")}
    )
    with pytest.raises(AuthError, match="python -m googleapps auth"):
        schedule(transport)
