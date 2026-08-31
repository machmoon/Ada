"""The Chat card: payload shape, and the routing honesty rule."""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.review import Severity

from googleapps import chat
from googleapps.tests.fakes import (
    WEBHOOK,
    RecordingTransport,
    fake_result,
    fake_route,
    finding,
)
from googleapps.transport import GoogleError, HttpResponse


def card_text(payload: dict) -> str:
    """Every string in the card, flattened, for containment assertions."""
    return json.dumps(payload, ensure_ascii=False)


# -- webhook validation ----------------------------------------------------


def test_the_real_webhook_shape_validates():
    assert chat.validate_webhook(WEBHOOK) == WEBHOOK


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/spaces/A/messages",
        "https://chat.googleapis.com.evil.example/v1/spaces/A/messages",
        "http://chat.googleapis.com/v1/spaces/A/messages",
        "https://chat.googleapis.com/not-a-webhook",
        "https://www.googleapis.com/v1/spaces/A/messages",
    ],
)
def test_non_webhook_urls_are_refused_before_sending(url):
    transport = RecordingTransport()
    with pytest.raises(GoogleError):
        chat.post_run_card(url, fake_result(), transport=transport)
    assert transport.requests == []


def test_a_refusal_never_repeats_the_webhook_secret():
    url = "https://evil.example/v1/spaces/SECRETSPACE/messages?token=hunter2token"
    with pytest.raises(GoogleError) as excinfo:
        chat.validate_webhook(url)
    assert "SECRETSPACE" not in str(excinfo.value)
    assert "hunter2token" not in str(excinfo.value)


# -- the card --------------------------------------------------------------


def test_the_card_posts_to_the_webhook_as_json():
    transport = RecordingTransport()
    chat.post_run_card(WEBHOOK, fake_result(), transport=transport)
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == WEBHOOK
    assert request.headers["Content-Type"].startswith("application/json")
    payload = json.loads(request.body)
    assert payload["cardsV2"][0]["card"]["sections"]
    assert payload["text"]  # the fallback line for notification surfaces


def test_counts_come_from_the_result_not_a_template():
    result = fake_result(
        findings=[
            finding(Severity.BLOCKER, "VIN has no bulk capacitor"),
            finding(Severity.NOTE, "Silkscreen overlaps a pad"),
        ]
    )
    text = card_text(chat.run_card(result))
    assert "2 finding(s): 1 blocker(s), 1 other(s)" in text
    assert "BLOCKER: VIN has no bulk capacitor" in text


def test_a_clean_fully_routed_run_says_board_ready():
    text = card_text(chat.run_card(fake_result()))
    assert "board ready" in text
    assert "2/2 nets routed" in text
    assert "unrouted" not in text.lower()


def test_unrouted_nets_are_named_verbatim_and_the_card_never_says_ready():
    """The honesty rule: a card must not say "routed" over a ratsnest."""
    result = fake_result(
        route=fake_route(
            unrouted={"SWD_CLK": "no path at 0.25 mm clearance",
                      "+3V3": "budget exhausted after 150000 expansions"}
        )
    )
    text = card_text(chat.run_card(result))
    assert "unrouted SWD_CLK: no path at 0.25 mm clearance" in text
    assert "unrouted +3V3: budget exhausted after 150000 expansions" in text
    assert "2/4 nets routed" in text
    assert "board ready" not in text
    assert "2 net(s) left unrouted" in text


def test_a_run_without_routing_says_so_instead_of_claiming_copper():
    text = card_text(chat.run_card(fake_result(route=None)))
    assert "not routed (routing was turned off for this run)" in text
    assert "nets routed" not in text


def test_blockers_change_the_verdict():
    result = fake_result(findings=[finding()])
    header = chat.run_card(result)["cardsV2"][0]["card"]["header"]
    assert "needs review — 1 blocker(s)" in header["title"]


def test_stage_lines_appear_when_provided():
    payload = chat.run_card(fake_result(), stage_lines=["place: done in 1.2 s"])
    assert "place: done in 1.2 s" in card_text(payload)


def test_an_http_error_from_the_webhook_raises_masked():
    transport = RecordingTransport(
        {"chat.googleapis.com": HttpResponse(403, b"{}")}
    )
    with pytest.raises(GoogleError) as excinfo:
        chat.post_run_card(WEBHOOK, fake_result(), transport=transport)
    assert "http_403" in str(excinfo.value)
    assert WEBHOOK not in str(excinfo.value)
