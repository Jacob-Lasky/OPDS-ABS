"""Logging setup and the per-request access log.

One line per request, written by ``AccessLogMiddleware`` at INFO:

    req=3f9a1c2e GET /opds/alice/libraries/<id>/search?q=dune -> 200 14383B 212ms
        user=alice auth=cache ip=203.0.113.7 ua="Marvin/3.6"

That line is the primary diagnostic for "it doesn't work on her reader": it says
which client asked for what, whether it authenticated and how, and what came
back. Feed generators add detail lines (search terms, result counts) tagged
with the same ``req=`` id.

DO NOT re-enable uvicorn's access log. It prints the raw query string, which is
where tokens and API keys travel; this middleware redacts them first.
"""
import contextvars
import logging
import sys
import time
import uuid
from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opds_abs.config import LOG_FORMAT, LOG_LEVEL

# Request id of the request being handled, for log lines emitted deeper down.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

SENSITIVE_PARAMS = frozenset({"token", "api_key", "apikey", "password", "access_token"})

access_logger = logging.getLogger("opds_abs.access")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record as ``%(request_id)s``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging() -> None:
    """Configure root logging once, for both the app and uvicorn.

    ``OPDS_LOG_LEVEL`` (default INFO) sets the level for ``opds_abs.*``;
    third-party loggers stay at WARNING so DEBUG is about this app.
    ``OPDS_LOG_FORMAT=plain`` drops timestamps for log collectors that add them.
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    if LOG_FORMAT == "plain":
        fmt = "%(levelname)-7s req=%(request_id)s %(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)-7s req=%(request_id)s %(name)s: %(message)s"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S%z"))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("opds_abs").setLevel(level)

    # uvicorn installs its own handlers; route its error log through ours and
    # silence its access log (see module docstring).
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
        lg.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True


def redact_query(query_string: str) -> str:
    """Return the query string with sensitive values replaced by ``REDACTED``."""
    if not query_string:
        return ""
    pairs = parse_qsl(query_string, keep_blank_values=True)
    return urlencode([(k, "REDACTED" if k.lower() in SENSITIVE_PARAMS else v) for k, v in pairs])


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


class AccessLogMiddleware:
    """Pure ASGI middleware: request id, timing and one access line per request.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so streamed downloads are not
    buffered, and so the byte count reflects what was actually sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex[:8]
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        start = time.perf_counter()
        status = 500
        sent = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            access_logger.exception("Unhandled error")
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            state = scope.get("state", {})
            query = redact_query(scope.get("query_string", b"").decode("latin-1"))
            path = scope.get("path", "") + (f"?{query}" if query else "")
            forwarded = _header(scope, b"x-forwarded-for").split(",")[0].strip()
            client = forwarded or (scope.get("client") or ("-",))[0]
            level = logging.WARNING if status >= 500 else logging.INFO
            access_logger.log(
                level,
                '%s %s -> %d %dB %.0fms user=%s auth=%s ip=%s ua="%s"',
                scope.get("method", "-"), path, status, sent, elapsed_ms,
                state.get("auth_user") or "-", state.get("auth_method") or "-",
                client, _header(scope, b"user-agent") or "-",
            )
            request_id_var.reset(token)
