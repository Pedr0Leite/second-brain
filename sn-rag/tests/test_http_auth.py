"""ADR-0006: authenticated HTTP transport, and the writer kept off it.

These tests exist because every failure mode here is silent-by-default: an
unauthenticated server looks identical to an authenticated one until someone
scans the port, and a registered `sn_ingest` looks identical to an absent one
until someone calls it. Assertions are on the actual surface and the actual
response, not on configuration flags.
"""
import pytest

from mcp_server import http_serve as H


# --- bind address ---------------------------------------------------------

def test_http_without_bind_is_refused():
    """--bind is required. The old code defaulted to 0.0.0.0 implicitly."""
    with pytest.raises(H.ConfigError) as e:
        H.parse_serve_args(["--http"])
    assert "--bind" in str(e.value)


def test_bind_all_interfaces_is_refused_explicitly():
    with pytest.raises(H.ConfigError) as e:
        H.parse_serve_args(["--http", "--bind", "0.0.0.0"])
    assert "0.0.0.0" in str(e.value)


def test_bind_missing_value_is_refused():
    with pytest.raises(H.ConfigError):
        H.parse_serve_args(["--http", "--bind"])
    with pytest.raises(H.ConfigError):
        H.parse_serve_args(["--http", "--bind", "--port"])


def test_valid_http_args_parse():
    cfg = H.parse_serve_args(["--http", "--bind", "127.0.0.1", "--port", "9000"])
    assert (cfg.transport, cfg.host, cfg.port) == ("http", "127.0.0.1", 9000)


def test_port_defaults_and_rejects_garbage():
    assert H.parse_serve_args(["--http", "--bind", "127.0.0.1"]).port == 8079
    with pytest.raises(H.ConfigError):
        H.parse_serve_args(["--http", "--bind", "127.0.0.1", "--port", "abc"])


def test_no_flags_is_stdio():
    assert H.parse_serve_args([]).transport == "stdio"


# --- token ----------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"SN_RAG_TOKEN": ""}, {"SN_RAG_TOKEN": "   "}])
def test_missing_or_empty_token_refuses_to_start(env):
    with pytest.raises(H.ConfigError) as e:
        H.require_token(env)
    assert "SN_RAG_TOKEN" in str(e.value)


def test_short_token_refuses_to_start():
    with pytest.raises(H.ConfigError):
        H.require_token({"SN_RAG_TOKEN": "short"})


def test_valid_token_returned_stripped():
    assert H.require_token({"SN_RAG_TOKEN": "  " + "k" * 32 + "  "}) == "k" * 32


@pytest.mark.parametrize("presented", [
    None, "", "Bearer", "Bearer ", "secret-token-value-32-chars-long",
    "Basic secret-token-value-32-chars", "Bearer wrong-token-entirely-here",
])
def test_bad_authorization_headers_all_rejected(presented):
    assert H.token_matches(presented, "secret-token-value-32-chars-long") is False


def test_correct_token_accepted_case_insensitive_scheme():
    tok = "secret-token-value-32-chars-long"
    assert H.token_matches(f"Bearer {tok}", tok) is True
    assert H.token_matches(f"bearer {tok}", tok) is True


# --- tool surface ---------------------------------------------------------

def test_http_surface_excludes_the_writer_and_stdio_includes_it():
    """The security boundary is the tool list itself, so assert on it."""
    from mcp_server import server as S

    http_names = S.register_tools("http")
    assert "sn_ingest" not in http_names, "writer must never be advertised over HTTP"
    assert len(http_names) == 6
    for expected in ("sn_search", "sn_get_section", "sn_outline",
                     "sn_lexical", "sn_research", "sn_stats"):
        assert expected in http_names

    stdio_names = S.register_tools("stdio")
    assert "sn_ingest" in stdio_names
    assert len(stdio_names) == 7


# --- middleware behaviour over a real ASGI transport ----------------------

def _probe_app(token: str):
    """Minimal ASGI app behind the real middleware, so we test the middleware
    rather than a re-implementation of it."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(request):
        return JSONResponse({"reached": True})

    app = Starlette(routes=[Route("/mcp", ok), Route("/healthz", ok)])
    app.add_middleware(H.build_auth_middleware(token))
    return app


def _get(app, path: str, headers: dict | None = None):
    """One request through the real ASGI stack. httpx's ASGITransport is
    async-only, so drive it with asyncio.run rather than taking a
    pytest-asyncio dependency for three tests."""
    import asyncio
    httpx = pytest.importorskip("httpx")

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://sn-rag.test") as c:
            return await c.get(path, headers=headers or {})

    return asyncio.run(go())


@pytest.mark.parametrize("headers,expected", [
    ({}, 401),
    ({"Authorization": "Bearer wrong-token-that-is-long"}, 401),
    ({"Authorization": "Basic " + "t" * 32}, 401),
    ({"Authorization": "Bearer " + "t" * 32}, 200),
])
def test_middleware_enforces_token_on_mcp_path(headers, expected):
    r = _get(_probe_app("t" * 32), "/mcp", headers)
    assert r.status_code == expected
    if expected == 401:
        assert "www-authenticate" in {k.lower() for k in r.headers}
        # The body must not echo anything about why it failed.
        assert "token" not in r.text.lower()
    else:
        assert r.json() == {"reached": True}


def test_healthz_is_public_but_mcp_is_not():
    app = _probe_app("t" * 32)
    assert _get(app, "/healthz").status_code == 200
    assert _get(app, "/mcp").status_code == 401


def test_unauthenticated_request_is_not_logged_with_the_token(caplog):
    """A rejected credential must not end up in the journal."""
    secret = "s" * 32
    with caplog.at_level("WARNING"):
        _get(_probe_app("t" * 32), "/mcp", {"Authorization": f"Bearer {secret}"})
    assert caplog.text, "a 401 must be logged"
    assert secret not in caplog.text
