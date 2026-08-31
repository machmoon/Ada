"""``silkscreen serve``: start the HTTP service and open it in a browser.

Thin on purpose. The server itself is ``service.app`` -- this module only does
the three things that stand between "the module exists" and "the app is open":

* Load ``.env``. The service deliberately does not read it (only the CLIs do),
  so without this step a key sitting in .env produces a service that answers
  every request with a missing-key 502, which reads as an outage.
* Resolve the port once, from ``--port`` or ``PORT``, and say what happened
  when the bind fails -- 8080 is routinely already taken.
* Open the browser, since "runs in one command" means the app is on screen.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from .onboard import load_dotenv, repo_root

__all__ = ["main"]

DEFAULT_PORT = 8080


def _import_service(root: Path):
    """Import ``service.app`` out of the checkout.

    ``service/`` is not part of the installed package (setuptools packages only
    ``engine/``), so it can only be reached through the repository on disk. A
    wheel installed away from its source tree therefore cannot serve, and
    saying so beats an ImportError traceback about a module nobody mentioned.
    """
    if not (root / "service" / "app.py").exists():
        raise SystemExit(
            f"error: no service/app.py under {root}.\n"
            "'silkscreen serve' runs the service out of the repository; "
            "install it editable with scripts/install.sh and serve from there."
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from service.app import make_server  # noqa: PLC0415 - after sys.path setup

    return make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen serve",
        description="Run the Silkscreen API and web UI, and open a browser at it.",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help=f"port to bind (default: $PORT, else {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--host", default="localhost",
        help="hostname to open in the browser (default: %(default)s); "
             "the server always binds 0.0.0.0",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="bind and serve without opening a browser",
    )
    args = parser.parse_args(argv)

    root = repo_root()

    # Before anything imports the service: app.py reads its configuration from
    # os.environ at request time, and never looks at .env itself.
    load_dotenv(root / ".env")

    port = args.port if args.port is not None else int(os.getenv("PORT", DEFAULT_PORT))
    # make_server() re-reads PORT when handed None; we always pass the resolved
    # value, so this only keeps the environment honest for anything downstream
    # that reports the port back (the container's own health checks do).
    os.environ["PORT"] = str(port)

    make_server = _import_service(root)

    try:
        server = make_server(port)
    except OSError as exc:
        print(f"error: could not bind port {port}: {exc}", file=sys.stderr)
        print(f"try:   silkscreen serve --port {port + 1}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{server.server_port}/"

    dist = Path(os.getenv("SILKSCREEN_WEB_DIST") or root / "frontend" / "dist")
    if (dist / "index.html").exists():
        print(f"web UI  {url}")
    else:
        # Serving the API alone is a legitimate mode, not an error -- but a
        # blank page at the root would otherwise look like a broken install.
        print(f"web UI  not built ({dist} has no index.html); serving the API only")
        print("        build it with: cd frontend && npm ci && npm run build")

    print(f"API     POST {url}generate   |   GET {url}healthz")
    if not os.getenv("GOOGLE_API_KEY"):
        # Generation is the whole point, so warn plainly rather than letting
        # the first request come back as a 502 the user has to decode.
        print("note    no GOOGLE_API_KEY set; /generate will fail. "
              "Run 'silkscreen setup' to store one.")
    # serve_forever() then blocks indefinitely, so a block-buffered stdout (any
    # pipe: `silkscreen serve | tee`, a supervisor, a CI log) would hold this
    # whole banner back until the process is killed. Flush it out now.
    print("Ctrl-C to stop.", flush=True)

    if not args.no_browser:
        # The socket is already bound and listening, so anything the browser
        # sends before serve_forever() starts waits in the accept backlog.
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
