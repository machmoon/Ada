"""The HTTP surface Slack calls.

A stdlib server, like ``service/app.py``, exposing:

* ``POST /slack/events``   — the Events API: URL verification and ``app_mention``
* ``POST /slack/commands`` — a slash command (``/silkscreen …``)
* ``GET  /healthz``        — liveness

Every request is signature-verified before it is parsed, and acknowledged
within Slack's three-second window before any work starts. The work then
happens on a worker thread and reports itself into the thread it came from,
which is why the acknowledgement can be immediate and still honest: it promises
a thread, not a result.

Run it::

    SLACK_BOT_TOKEN=xoxb-… SLACK_SIGNING_SECRET=… GOOGLE_API_KEY=… \\
        python -m slackbot
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .commands import CommandError, parse_command
from .config import Config, ConfigError, load_config
from .runner import Runner
from .slack import SlackClient, verify_signature

__all__ = ["Dispatcher", "make_handler", "make_server", "main", "MAX_BODY_BYTES"]

log = logging.getLogger("slackbot.app")

MAX_BODY_BYTES = 1 << 20
#: How long a queued run waits for a slot before the channel is told the bot is
#: busy. Long enough to absorb one run ahead of it, short enough that nobody
#: sits watching a thread that will never answer.
SLOT_WAIT_S = 240.0


class _SeenEvents:
    """Bounded set of event ids already handled.

    Slack retries an event up to three times when it does not get a prompt 200,
    and a retry that slips through is a second paid pipeline run for one
    request. The retry header alone is not enough -- a slow first response and
    a genuine retry look identical from here -- so ids are remembered.
    """

    def __init__(self, limit: int = 1024):
        self._limit = limit
        self._lock = threading.Lock()
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, event_id: str) -> bool:
        if not event_id:
            return True
        with self._lock:
            if event_id in self._ids:
                return False
            self._ids[event_id] = None
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)
            return True


class Dispatcher:
    """Decides what an incoming Slack payload means, and runs it off-thread."""

    def __init__(
        self, config: Config, runner: Runner, *, slot_wait_s: float = SLOT_WAIT_S
    ):
        self.config = config
        self.runner = runner
        self.slot_wait_s = slot_wait_s
        self._seen = _SeenEvents()
        self._threads: list[threading.Thread] = []

    # -- events -----------------------------------------------------------

    def handle_event_payload(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Map one Events API body to a response, scheduling any work."""
        kind = payload.get("type")
        if kind == "url_verification":
            # Answered inline, in plain text: this is the one Slack request
            # that wants a body rather than an acknowledgement.
            return 200, {"challenge": str(payload.get("challenge", ""))}
        if kind != "event_callback":
            return 200, {"ok": True}

        if not self._seen.add_if_new(str(payload.get("event_id", ""))):
            log.info("ignoring duplicate delivery of %s", payload.get("event_id"))
            return 200, {"ok": True}

        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return 200, {"ok": True}
        if event.get("type") != "app_mention":
            return 200, {"ok": True}
        # Never answer ourselves, or any other bot: two silkscreen bots in one
        # channel would otherwise mention each other indefinitely.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return 200, {"ok": True}

        channel = str(event.get("channel", ""))
        if not channel or not self.config.channel_allowed(channel):
            log.info("ignoring mention in disallowed channel %s", channel)
            return 200, {"ok": True}

        # Replying in the existing thread if there is one keeps a follow-up
        # ("review", "order") attached to the run it refers to.
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        self._schedule(
            text=str(event.get("text", "")),
            channel=channel,
            thread_ts=thread_ts,
            user=str(event.get("user", "")),
        )
        return 200, {"ok": True}

    # -- slash commands ---------------------------------------------------

    def handle_slash_payload(self, form: dict[str, str]) -> tuple[int, Any]:
        """Map one slash-command body to its immediate acknowledgement.

        A slash command has no message to thread under, so the bot posts a
        visible message into the channel first and threads the run beneath it.
        The team seeing the request is the point; an ephemeral-only run would
        put the result where only one person could read it.
        """
        channel = form.get("channel_id", "")
        if not channel or not self.config.channel_allowed(channel):
            return 200, {
                "response_type": "ephemeral",
                "text": "silkscreen isn't enabled in this channel.",
            }
        self._schedule(
            text=form.get("text", ""),
            channel=channel,
            thread_ts="",
            user=form.get("user_id", ""),
        )
        return 200, {
            "response_type": "ephemeral",
            "text": "Starting — I'll post the run in this channel.",
        }

    # -- scheduling -------------------------------------------------------

    def _schedule(self, *, text: str, channel: str, thread_ts: str, user: str) -> None:
        try:
            command = parse_command(text)
        except CommandError as exc:
            # Parse errors are cheap and need no slot; they answer immediately.
            message = str(exc)
            self._spawn(
                lambda: self.runner.report_error(channel, thread_ts, message)
            )
            return
        self._spawn(
            lambda: self._run_with_slot(command, channel, thread_ts, user)
        )

    def _run_with_slot(
        self, command: Any, channel: str, thread_ts: str, user: str
    ) -> None:
        cheap = command.verb in ("help", "order")
        if cheap:
            self.runner.handle(command, channel=channel, thread_ts=thread_ts, user=user)
            return
        if not self.runner.acquire_slot(timeout=self.slot_wait_s):
            self.runner.report_busy(channel, thread_ts)
            return
        try:
            self.runner.handle(command, channel=channel, thread_ts=thread_ts, user=user)
        finally:
            self.runner.release_slot()

    def _spawn(self, work: Any) -> threading.Thread:
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._threads.append(thread)
        # Keep the list from growing for the lifetime of the process.
        self._threads = [t for t in self._threads if t.is_alive()]
        return thread

    def join(self, timeout: float = 30.0) -> None:
        """Wait for scheduled work. For tests and shutdown, not the hot path."""
        for thread in list(self._threads):
            thread.join(timeout)


def make_handler(dispatcher: Dispatcher) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one dispatcher."""

    config = dispatcher.config

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "silkscreen-slack"

        # -- plumbing ----------------------------------------------------

        def _send(self, code: int, payload: Any, content_type: str = "") -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload).encode("utf-8")
                content_type = content_type or "application/json; charset=utf-8"
            else:
                body = str(payload).encode("utf-8")
                content_type = content_type or "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, "bad Content-Length")
                return None
            if length > MAX_BODY_BYTES:
                # Drain before answering. Replying to a keep-alive request
                # whose body is still in flight makes the client see a reset
                # connection instead of the 413 explaining what went wrong.
                self._drain(length)
                self.close_connection = True
                self._send(413, "body too large")
                return None
            return self.rfile.read(length) if length > 0 else b""

        def _drain(self, length: int, chunk: int = 64 << 10) -> None:
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(chunk, remaining))
                if not block:
                    return
                remaining -= len(block)

        def _verified_body(self) -> bytes | None:
            """Read the body and prove it came from Slack, or answer 401.

            Verification happens against the raw bytes, before any parsing.
            Parsing first and verifying after would mean acting on a forged
            body's shape even when the signature is rejected.
            """
            body = self._read_body()
            if body is None:
                return None
            ok = verify_signature(
                config.signing_secret,
                timestamp=self.headers.get("X-Slack-Request-Timestamp", ""),
                signature=self.headers.get("X-Slack-Signature", ""),
                body=body,
            )
            if not ok:
                log.warning("rejected an unsigned or stale request to %s", self.path)
                self._send(401, "bad signature")
                return None
            return body

        # -- routes ------------------------------------------------------

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/healthz":
                self._send(200, {"ok": True, "service": "silkscreen-slack"})
                return
            self._send(404, "not found")

        def do_POST(self) -> None:
            route = self.path.split("?")[0]
            if route == "/slack/events":
                self._events()
            elif route == "/slack/commands":
                self._slash()
            else:
                self._send(404, "not found")

        def _events(self) -> None:
            body = self._verified_body()
            if body is None:
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, "body was not JSON")
                return
            if not isinstance(payload, dict):
                self._send(400, "expected a JSON object")
                return
            code, response = dispatcher.handle_event_payload(payload)
            self._send(code, response)

        def _slash(self) -> None:
            body = self._verified_body()
            if body is None:
                return
            parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            form = {k: v[0] for k, v in parsed.items() if v}
            code, response = dispatcher.handle_slash_payload(form)
            self._send(code, response)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(config: Config, runner: Runner | None = None) -> ThreadingHTTPServer:
    client = SlackClient(config.bot_token)
    dispatcher = Dispatcher(config, runner or Runner(config, client))
    return ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(dispatcher))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server = make_server(config)
    for key, value in config.redacted().items():
        log.info("config %s = %s", key, value)
    log.info("listening on :%s (POST /slack/events)", server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
