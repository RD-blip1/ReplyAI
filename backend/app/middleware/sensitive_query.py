import logging

from starlette.types import ASGIApp, Receive, Scope, Send

GOOGLE_CALLBACK_PATH = "/auth/google/callback"
REDACTED_QUERY = "<redacted>"


class SensitiveOAuthQueryRedactionMiddleware:
    """Clear the callback query from the ASGI scope after the route has read it.

    This is defence in depth for anything that inspects the scope once a request has
    completed. It does not, on its own, redact uvicorn's access log: uvicorn builds its
    access line from the raw scope while the response is being sent, which is before the
    ``finally`` below runs. The access-log filter is what covers that path.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != GOOGLE_CALLBACK_PATH:
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            scope["query_string"] = b""


class SensitiveOAuthAccessLogFilter(logging.Filter):
    """Redact the OAuth callback query string from uvicorn's access log record.

    Uvicorn logs its access line from the request scope as the response is sent, before
    the ASGI middleware that clears ``scope["query_string"]`` can run, so the Google
    authorization code and state would otherwise be written to the log. This filter runs
    on the log record instead: the method, path and status are kept, and only the query
    string is replaced, so application diagnostics are preserved without exposing the
    sensitive parameters.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not (isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str)):
            return True
        path = args[2]
        if path == GOOGLE_CALLBACK_PATH or path.startswith(GOOGLE_CALLBACK_PATH + "?"):
            record.args = (
                args[0],
                args[1],
                f"{GOOGLE_CALLBACK_PATH}?{REDACTED_QUERY}",
                args[3],
                args[4],
            )
        return True
