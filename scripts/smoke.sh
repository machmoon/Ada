#!/usr/bin/env bash
# Post-deploy smoke check against a live Silkscreen service.
#
#     SILKSCREEN_ACCESS_TOKEN=... ./scripts/smoke.sh https://silkscreen-xxxx.run.app
#     ./scripts/smoke.sh --dry-run https://...        # print the plan, call nothing
#     ./scripts/smoke.sh --health-only https://...    # GET /healthz and stop
#     ./scripts/smoke.sh --help
#
# Two checks, in order: GET /healthz must answer {"ok": true}, then one real
# POST /generate must come back with every field the desktop client reads. The
# second one spends a Gemini call and a CP-SAT solve -- that is the point. A
# service that answers /healthz and then 500s on the only route anyone uses is
# exactly the failure this check exists to catch.
#
# The token is read from the environment and never printed, never logged, and
# never passed on a command line (argv is world-readable in `ps`). It reaches
# curl through a private, mode-600 temporary config file that is deleted on
# exit, including on failure.
#
# A service deployed without --allow-unauthenticated answers 401/403 at Cloud
# Run's own IAM edge before the app sees the request. The app never gates GET,
# so that status on /healthz is recognised as the edge, retried with a gcloud
# identity token, and -- because the identity token occupies the Authorization
# header the app token would need -- the app-token assertions are then SKIPPED
# out loud rather than faked.
set -euo pipefail

URL=""
TOKEN="${SILKSCREEN_ACCESS_TOKEN:-}"
INTENT="${SILKSCREEN_SMOKE_INTENT:-an stm32 blinky board with one led and a decoupling capacitor}"
HEALTH_ONLY=0
DRY_RUN=0
TIMEOUT="${SILKSCREEN_SMOKE_TIMEOUT:-900}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }

die() {
    printf '\nFAIL: %s\n' "$1" >&2
    if [ $# -gt 1 ]; then
        printf 'hint: %s\n' "$2" >&2
    fi
    exit 1
}

usage() {
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'OPTS'

Options:
  --health-only   run only the free GET /healthz check
  --intent TEXT   the prompt to generate (default: a small STM32 board)
  --timeout SECS  per-request timeout (default: 900)
  --dry-run       print what would be requested; make no HTTP calls
  -h, --help      this text

Environment:
  SILKSCREEN_ACCESS_TOKEN   bearer token, if the service was deployed with one
OPTS
}

while [ $# -gt 0 ]; do
    case "$1" in
        --health-only) HEALTH_ONLY=1; shift ;;
        --intent)      INTENT="${2:?--intent needs a value}"; shift 2 ;;
        --timeout)     TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        -*)            die "unknown option: $1" "./scripts/smoke.sh --help" ;;
        *)             URL="$1"; shift ;;
    esac
done

[ -n "$URL" ] || die "no service URL given" \
    "./scripts/smoke.sh https://silkscreen-xxxx-uc.a.run.app"

# A trailing slash would build //healthz, which the router answers with a 404.
URL="${URL%/}"

case "$URL" in
    https://*) ;;
    http://127.0.0.1*|http://localhost*)
        say "note: plain http against localhost -- fine for a local check, never for a deployed URL" ;;
    *) die "refusing to send a bearer token over '$URL'" \
           "use the https:// URL that scripts/deploy.sh printed" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is not on PATH" "install curl"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH" \
    "install Python 3.11+ -- the response assertions are a python3 -c one-liner"

say "Silkscreen smoke check"
say "target: $URL"
if [ -n "$TOKEN" ]; then
    say "auth:   bearer token from SILKSCREEN_ACCESS_TOKEN (not printed)"
else
    say "auth:   none. If the service was deployed with a token, /generate will 401."
fi
[ "$DRY_RUN" -eq 1 ] && say "mode:   dry run (no HTTP calls)"

# All temporary files (curl configs carrying tokens, response bodies) live in
# one private mode-700 directory removed on every exit path.
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
chmod 700 "$WORKDIR"

# 1 once /healthz proved to be behind Cloud Run IAM (--no-allow-unauthenticated).
IAM_MODE=0
ID_TOKEN=""

# ---------------------------------------------------------------- healthz ----
step "GET $URL/healthz"

if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: curl -sS --max-time 30 '$URL/healthz'"
    say "  (a 401/403 from Cloud Run's IAM edge would be retried with a gcloud identity token)"
else
    HEALTH_FILE="$WORKDIR/healthz.json"
    HEALTH_CODE="$(curl -sS --max-time 30 "$URL/healthz" \
        -o "$HEALTH_FILE" -w '%{http_code}' 2>/dev/null || true)"

    if [ "$HEALTH_CODE" = "401" ] || [ "$HEALTH_CODE" = "403" ]; then
        # The app never gates GET, so this refusal is Cloud Run's own IAM edge
        # (a deploy without --allow-unauthenticated), answered before the
        # request ever reaches the container. Retry as the gcloud caller.
        say "  note: anonymous GET got HTTP $HEALTH_CODE. The app never gates GET, so this"
        say "        is Cloud Run's IAM edge; retrying with a gcloud identity token."
        command -v gcloud >/dev/null 2>&1 || die \
            "the service is IAM-gated and gcloud is not on PATH" \
            "install the gcloud CLI, or deploy with --allow-unauthenticated"
        ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
        [ -n "$ID_TOKEN" ] || die "gcloud could not mint an identity token" \
            "gcloud auth login, then re-run"
        IAM_CONFIG="$WORKDIR/iam-curl.cfg"
        touch "$IAM_CONFIG"
        chmod 600 "$IAM_CONFIG"
        printf 'header = "Authorization: Bearer %s"\n' "$ID_TOKEN" > "$IAM_CONFIG"
        HEALTH_CODE="$(curl -sS --max-time 30 --config "$IAM_CONFIG" "$URL/healthz" \
            -o "$HEALTH_FILE" -w '%{http_code}' 2>/dev/null || true)"
        [ "$HEALTH_CODE" = "200" ] && IAM_MODE=1
    fi

    ROOT_PROBE=0
    if [ "$HEALTH_CODE" = "404" ]; then
        # Google's frontend intercepts /healthz on run.app domains at the edge
        # and answers 404 before the request ever reaches the container
        # (observed on the 2026-08-31 deployment). GET / is served by the app
        # itself -- the web bundle, or the JSON liveness line -- so probe that.
        say "  note: /healthz answered 404. On run.app domains Google's edge intercepts"
        say "        that path before the container sees it; probing GET / instead."
        ROOT_PROBE=1
        if [ "$IAM_MODE" -eq 1 ]; then
            HEALTH_CODE="$(curl -sS --max-time 30 --config "$IAM_CONFIG" "$URL/" \
                -o "$HEALTH_FILE" -w '%{http_code}' 2>/dev/null || true)"
        else
            HEALTH_CODE="$(curl -sS --max-time 30 "$URL/" \
                -o "$HEALTH_FILE" -w '%{http_code}' 2>/dev/null || true)"
        fi
    fi

    case "$HEALTH_CODE" in
        200) ;;
        ""|000)
            die "no response from $URL/healthz" \
                "check the service exists: gcloud run services describe ... --format='value(status.url)'" ;;
        *)
            die "liveness probe returned HTTP $HEALTH_CODE" "curl -i '$URL/healthz' ; curl -i '$URL/'" ;;
    esac
    if [ "$ROOT_PROBE" -eq 1 ]; then
        pass "GET / answered 200 (edge intercepts /healthz on this domain)"
    else
    python3 -c '
import json, sys
try:
    body = json.load(open(sys.argv[1], "rb"))
except json.JSONDecodeError:
    raise SystemExit("/healthz did not return JSON")
if body.get("ok") is not True:
    raise SystemExit("/healthz returned %r, expected {\"ok\": true}" % (body,))
' "$HEALTH_FILE" || die "/healthz did not answer {\"ok\": true}" "curl -i '$URL/healthz'"
    fi
    if [ "$IAM_MODE" -eq 1 ]; then
        [ "$ROOT_PROBE" -eq 1 ] || pass '{"ok": true} (via IAM identity token)'
        say "  note: this service is IAM-gated. The identity token occupies the"
        say "        Authorization header, which is the same header the app-token"
        say "        gate reads -- the two gates cannot stack on one request, so"
        say "        the app-token assertions below cannot be exercised here."
    else
        [ "$ROOT_PROBE" -eq 1 ] || pass '{"ok": true}'
    fi
fi

if [ "$HEALTH_ONLY" -eq 1 ]; then
    step "Smoke check passed (health only)"
    exit 0
fi

# --------------------------------------------------------------- generate ----
# One real board. This spends a model call and a solve, so it is the last thing
# the script does and the one it reports loudest about.
step "POST $URL/generate"
say "  intent: $INTENT"

REQ="$(python3 -c 'import json,sys; print(json.dumps({"intent": sys.argv[1]}))' "$INTENT")"

if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: curl -sS --max-time $TIMEOUT -X POST '$URL/generate'"
    say "             -H 'Content-Type: application/json' -H 'Authorization: Bearer <redacted>'"
    say "             --data '$REQ'"
    say ""
    say "Dry run complete. Nothing was requested."
    exit 0
fi

# Tokens go in through a curl --config file rather than a -H argument: argv is
# world-readable via `ps` on a shared machine, and a CI runner would echo it
# into the job log. The file is mode 600 inside the mode-700 WORKDIR that the
# trap removes on every exit path, including the failure ones below.
#
# In IAM mode the Authorization header must carry the Google identity token or
# Cloud Run's edge refuses the request -- which means it cannot also carry the
# app's bearer token. The app-token assertions are skipped out loud.
CURL_CONFIG="$WORKDIR/curl.cfg"
touch "$CURL_CONFIG"
chmod 600 "$CURL_CONFIG"
{
    printf 'header = "Content-Type: application/json"\n'
    if [ "$IAM_MODE" -eq 1 ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$ID_TOKEN"
    elif [ -n "$TOKEN" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$TOKEN"
    fi
} > "$CURL_CONFIG"

if [ "$IAM_MODE" -eq 1 ] && [ -n "$TOKEN" ]; then
    say "  SKIPPED: app-token check. SILKSCREEN_ACCESS_TOKEN is set, but the"
    say "           Authorization header already carries the IAM identity token,"
    say "           so the app token cannot be presented on the same request."
fi

BODY_FILE="$WORKDIR/generate.json"

HTTP_CODE="$(curl -sS --max-time "$TIMEOUT" -X POST "$URL/generate" \
    --config "$CURL_CONFIG" \
    --data "$REQ" \
    -o "$BODY_FILE" -w '%{http_code}' || true)"

case "$HTTP_CODE" in
    200) ;;
    401|403)
        if [ "$IAM_MODE" -eq 1 ]; then
            say ""
            say "  SKIPPED: POST /generate answered HTTP $HTTP_CODE behind IAM."
            say "           The app-token gate is on and refused the identity token,"
            say "           which is correct -- but the app token cannot ride the same"
            say "           Authorization header, so /generate cannot be exercised by"
            say "           this probe at all. Not a pass: only /healthz was verified."
            step "Smoke check finished with skips (IAM-gated service)"
            say "  /healthz is live; the /generate checks were SKIPPED, not passed."
            say "  To smoke /generate, deploy with --allow-unauthenticated and the"
            say "  app token as the gate, or temporarily unset the app token."
            exit 0
        fi
        die "POST /generate returned $HTTP_CODE" \
            "the service requires a bearer token; export SILKSCREEN_ACCESS_TOKEN and re-run" ;;
    ""|000)
        die "POST /generate produced no HTTP status (connection failed or timed out after ${TIMEOUT}s)" \
            "gcloud run services logs read <service> --region <region> --limit 50" ;;
    *)
        # The body may carry an error_id the logs can be searched by. It never
        # carries the token, so printing it is safe.
        say "  response body:"
        head -c 2000 "$BODY_FILE" >&2 || true
        printf '\n' >&2
        die "POST /generate returned HTTP $HTTP_CODE, expected 200" \
            "gcloud run services logs read <service> --region <region> --limit 50" ;;
esac
pass "HTTP 200"

# Every field the desktop client actually reads. Missing one is a broken
# deploy even when the status line says 200.
python3 - "$BODY_FILE" <<'PY' || die "POST /generate returned 200 but the body is not a usable board" "re-run with the body printed: curl ... | head -c 2000"
import json
import sys

with open(sys.argv[1], "rb") as handle:
    try:
        body = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"/generate did not return JSON: {exc}")

if not isinstance(body, dict):
    raise SystemExit("/generate returned %s, expected a JSON object" % type(body).__name__)

problems = []
for field in ("board_mm", "status", "parts", "kicad_pcb", "placements"):
    if field not in body:
        problems.append(f"missing field {field!r}")

board = body.get("board_mm")
if not (isinstance(board, list) and len(board) == 2 and all(
        isinstance(v, (int, float)) and v > 0 for v in board)):
    problems.append(f"board_mm is {board!r}, expected two positive numbers")

parts = body.get("parts")
if not (isinstance(parts, list) and parts):
    problems.append("parts is empty -- a board with no footprints is not a board")

pcb = body.get("kicad_pcb")
if not (isinstance(pcb, str) and pcb.lstrip().startswith("(kicad_pcb")):
    problems.append("kicad_pcb is not a KiCad board file")

placements = body.get("placements")
if not isinstance(placements, dict) or not placements.get("parts"):
    problems.append("placements carries no part geometry -- the board well would render empty")

status = body.get("status")
if not isinstance(status, str) or not status:
    problems.append(f"status is {status!r}")

if problems:
    raise SystemExit("\n".join("  - " + p for p in problems))

print(f"  ok    board_mm  {board[0]} x {board[1]} mm")
print(f"  ok    status    {status}")
print(f"  ok    parts     {len(parts)} footprints")
print(f"  ok    kicad_pcb {len(pcb)} bytes")
print(f"  ok    placements {len(placements['parts'])} placed parts")
PY

step "Smoke check passed"
say "  $URL is live and generating boards."
say "  Record the URL in README.md if it is not there yet."
