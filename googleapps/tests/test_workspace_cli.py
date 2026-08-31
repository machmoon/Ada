"""The subcommands: check stays local, run gates the review event on blockers."""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.review import Severity

import googleapps.__main__ as cli
from googleapps.config import Config
from googleapps.runner import RunOutcome, StageLog, email_body
from googleapps.tests.fakes import (
    WEBHOOK,
    RecordingTransport,
    config_with_token,
    fake_result,
    fake_route,
    finding,
)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A fully-configured environment with a valid token file on disk."""
    config = config_with_token(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    return config


def outcome(tmp_path, **kwargs):
    board = tmp_path / "out" / "board.kicad_pcb"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_bytes(b"(kicad_pcb (version 20240108))\n")
    kwargs.setdefault("board_path", board)
    return RunOutcome(
        result=fake_result(**kwargs),
        stage_lines=["place: done in 1.2 s"],
        duration_s=3.4,
    )


def wire_run(monkeypatch, tmp_path, **kwargs):
    run = outcome(tmp_path, **kwargs)
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **k: run)
    return run


# -- check -----------------------------------------------------------------


def test_check_is_purely_local_and_never_prints_a_secret(configured, capsys):
    transport = RecordingTransport()
    assert cli.main(["check"], transport=transport) == 0
    assert transport.requests == []
    out = capsys.readouterr().out
    assert "valid" in out
    assert "client-secret-value" not in out
    assert "AIza-key" not in out
    assert WEBHOOK not in out
    assert "ya29.live-token" not in out


def test_check_reports_a_missing_token_with_the_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "load_config", lambda: Config(token_path=tmp_path / "none.json")
    )
    assert cli.main(["check"], transport=RecordingTransport()) == 0
    out = capsys.readouterr().out
    assert "missing" in out
    assert "python -m googleapps auth" in out


# -- run: config gates before model spend ----------------------------------


def test_run_without_an_api_key_refuses_before_the_pipeline(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("the pipeline must not run unconfigured"),
    )
    assert cli.main(["run", "an LDO"], transport=RecordingTransport()) == 2
    assert "GOOGLE_API_KEY" in capsys.readouterr().err


def test_run_with_email_but_no_token_refuses_before_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli, "load_config",
        lambda: Config(google_api_key="k", token_path=tmp_path / "none.json"),
    )
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("a paid run must not start without a token"),
    )
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"], transport=RecordingTransport()
    )
    assert code == 2
    assert "python -m googleapps auth" in capsys.readouterr().err


def test_schedule_needs_an_attendee(configured, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("flag validation happens before the run"),
    )
    code = cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "b.kicad_pcb"), "--schedule"],
        transport=RecordingTransport(),
    )
    assert code == 2
    assert "--attendee" in capsys.readouterr().err


# -- run: delivery ---------------------------------------------------------


def test_a_plain_run_delivers_nothing(configured, monkeypatch, tmp_path, capsys):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    assert cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "out" / "board.kicad_pcb")],
        transport=transport,
    ) == 0
    assert transport.requests == []
    assert "board" in capsys.readouterr().out


def test_chat_and_email_each_reach_their_endpoint(
    configured, monkeypatch, tmp_path
):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--chat",
            "--email", "team@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert transport.called("chat.googleapis.com/v1/spaces")
    assert transport.called("gmail.googleapis.com")


def test_schedule_creates_no_event_when_the_review_is_clean(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(monkeypatch, tmp_path)  # no findings at all
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    assert "no blockers" in capsys.readouterr().out


def test_schedule_creates_the_event_only_because_of_blockers(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(
        monkeypatch, tmp_path,
        findings=[finding(Severity.BLOCKER, "VIN has no bulk capacitor"),
                  finding(Severity.NOTE, "informational")],
    )
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert transport.called("calendars/primary/events?conferenceDataVersion=1")
    body = json.loads(
        [r for r in transport.requests if "calendar" in r.url][0].body
    )
    assert body["attendees"] == [{"email": "lead@example.com"}]
    assert "1 blocker(s)" in capsys.readouterr().out


def test_a_marginal_only_review_does_not_schedule(
    configured, monkeypatch, tmp_path
):
    wire_run(monkeypatch, tmp_path, findings=[finding(Severity.MARGINAL, "tight")])
    transport = RecordingTransport()
    cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert not transport.called("calendar")


# -- the stage log and the email body --------------------------------------


def test_the_stage_log_only_ticks_stages_whose_events_arrived():
    log = StageLog()
    log.on_event({"event": "stage.start", "stage": "place", "t_s": 1.0})
    log.on_event({"event": "stage.start", "stage": "route", "t_s": 4.5})
    log.on_event({"event": "stage.done", "stage": "place", "t_s": 4.5})
    assert log.lines() == ["place: done in 3.5 s"]  # route never finished


def test_the_email_body_names_every_unrouted_net(tmp_path):
    run = outcome(
        tmp_path,
        route=fake_route(unrouted={"SWD_CLK": "no path at 0.25 mm clearance"}),
        findings=[finding()],
    )
    body = email_body(run)
    assert "SWD_CLK: no path at 0.25 mm clearance" in body
    assert "VIN has no bulk capacitor" in body
    assert "place: done in 1.2 s" in body
