"""Authenticated HTTP transport for the MCP server (ADR-0006).

Everything security-relevant here is a pure function so it can be tested without
opening a socket. The rules this module enforces:

  1. `--bind` is REQUIRED with `--http`. There is no default bind address.
     The previous implementation set `0.0.0.0` implicitly; a security-relevant
     default should have to be typed out, so an omitted `--bind` is a hard error
     rather than a fallback.
  2. `SN_RAG_TOKEN` must be present and non-empty or the server REFUSES TO START.
     An auth-optional server is one misconfiguration away from an open one.
  3. Token comparison uses `hmac.compare_digest`. Failures return 401 and are
     logged with the peer address, never with the presented token.
  4. `sn_ingest` is not registered at all under HTTP (enforced in server.py).
     A tool that was never advertised cannot be called.
"""
import hmac
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("sn_rag.http")

# Endpoints that must answer without a token. Kept deliberately tiny: a health
# check is useful for systemd/uptime probes and leaks nothing, but anything that
# touches the corpus stays behind auth.
PUBLIC_PATHS = frozenset({"/healthz"})


class ConfigError(Exception):
    """Refusing to start. Never downgraded to a warning."""


@dataclass(frozen=True)
class ServeConfig:
    transport: str          # "stdio" | "http"
    host: str = ""
    port: int = 0


def parse_serve_args(argv: list[str]) -> ServeConfig:
    """Parse transport arguments. Raises ConfigError rather than defaulting.

    `--http` alone is not enough: without `--bind` there is no address to serve
    on, and guessing one is exactly the mistake ADR-0006 exists to prevent.
    """
    if "--http" not in argv:
        return ServeConfig(transport="stdio")

    if "--bind" not in argv:
        raise ConfigError(
            "--http requires --bind ADDR (e.g. --bind 127.0.0.1, or the host's "
            "Tailscale address). There is no default: binding 0.0.0.0 would "
            "publish a private corpus to every interface."
        )

    i = argv.index("--bind") + 1
    if i >= len(argv) or argv[i].startswith("--"):
        raise ConfigError("--bind requires an address argument")
    host = argv[i]

    if host == "0.0.0.0":
        raise ConfigError(
            "--bind 0.0.0.0 is refused: it exposes the corpus on every "
            "interface. Bind a specific private address instead."
        )

    port = 8079
    if "--port" in argv:
        j = argv.index("--port") + 1
        if j >= len(argv):
            raise ConfigError("--port requires a number")
        try:
            port = int(argv[j])
        except ValueError:
            raise ConfigError(f"--port must be an integer, got {argv[j]!r}")

    return ServeConfig(transport="http", host=host, port=port)


def require_token(env: dict | None = None) -> str:
    """Return the bearer token, or refuse to start.

    Returning an empty string on a missing token would make the auth check pass
    trivially for a caller who also sends nothing, so absence is fatal here
    rather than at request time.
    """
    env = os.environ if env is None else env
    token = (env.get("SN_RAG_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "SN_RAG_TOKEN is unset or empty; refusing to start an unauthenticated "
            "HTTP server. Generate one with: python3 -c "
            "\"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if len(token) < 16:
        raise ConfigError(
            f"SN_RAG_TOKEN is {len(token)} characters; refusing to start with a "
            "token short enough to guess. Use at least 16."
        )
    return token


def token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time bearer comparison.

    Accepts the raw Authorization header value. A missing header, a non-Bearer
    scheme, or a mismatched token are all a plain False — the caller must not be
    able to distinguish them from the response.
    """
    if not presented:
        return False
    parts = presented.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def build_auth_middleware(expected_token: str):
    """Starlette middleware enforcing the bearer token before dispatch.

    Written as a bare ASGI middleware rather than a Starlette `BaseHTTPMiddleware`
    because streamable-http keeps long-lived streaming responses open, and
    BaseHTTPMiddleware buffers them.
    """
    from starlette.types import ASGIApp, Receive, Scope, Send

    class BearerAuthMiddleware:
        def __init__(self, app: "ASGIApp") -> None:
            self.app = app

        async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
            if scope["type"] != "http":
                # Reject non-HTTP scopes (websocket) outright: the MCP streamable
                # transport does not use them, so anything arriving here is
                # unexpected and must not bypass the token check.
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await self.app(scope, receive, send)
                return

            if scope.get("path") in PUBLIC_PATHS:
                await self.app(scope, receive, send)
                return

            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if not token_matches(headers.get("authorization"), expected_token):
                client = scope.get("client") or ("?", 0)
                # Peer address only. Logging the presented token would write a
                # credential into the journal.
                log.warning("401 unauthenticated MCP request from %s to %s",
                            client[0], scope.get("path"))
                await _send_401(send)
                return

            await self.app(scope, receive, send)

    return BearerAuthMiddleware


async def _send_401(send) -> None:
    body = b'{"error":"unauthorized"}'
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b'Bearer realm="sn-rag"'),
        ],
    })
    await send({"type": "http.response.body", "body": body})
