"""空闲 Cookie 保活。

回归背景：完全没流量时浏览器不会访问 Google，``__Secure-1PSIDTS`` 不再滚动，
闲置几小时后的第一个请求带着旧时间戳，Google 直接 403 / 空响应——表现就是
"隔一段时间就得重新导一次 cookie"。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session import BrowserSession


class _Page:
    def __init__(self, *, closed: bool = False, raises: bool = False) -> None:
        self.closed = closed
        self.raises = raises
        self.calls: list[tuple[str, dict[str, object]]] = []

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, expression: str, args: object = None, **kwargs: object):
        if self.raises:
            raise RuntimeError("cdp exploded")
        self.calls.append((expression, kwargs))
        return 200


def _session(page: object, *, in_flight: int = 0, auth_file: str | None = "auth.json"):
    sess = BrowserSession.__new__(BrowserSession)
    sess._gate = SimpleNamespace(active_context=SimpleNamespace(page=page))
    sess._in_flight = in_flight
    sess._auth_file = auth_file
    return sess


@pytest.mark.asyncio
async def test_keepalive_touches_page_and_writes_back(monkeypatch):
    page = _Page()
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(page).keepalive_cookies() is True

    assert len(page.calls) == 1
    expression, kwargs = page.calls[0]
    assert "credentials: 'include'" in expression
    assert "cache: 'no-store'" in expression
    assert kwargs["timeout_s"] == 12.0
    saved.assert_awaited_once_with(page, "auth.json")


@pytest.mark.asyncio
async def test_keepalive_skips_while_request_in_flight(monkeypatch):
    page = _Page()
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(page, in_flight=1).keepalive_cookies() is False

    assert page.calls == []
    saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_keepalive_skips_without_page(monkeypatch):
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(None).keepalive_cookies() is False
    saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_keepalive_skips_closed_page(monkeypatch):
    page = _Page(closed=True)
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(page).keepalive_cookies() is False
    saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_keepalive_survives_cdp_error(monkeypatch):
    page = _Page(raises=True)
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(page).keepalive_cookies() is False
    saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_keepalive_without_auth_file_still_touches(monkeypatch):
    page = _Page()
    saved = AsyncMock()
    monkeypatch.setattr(session_module, "save_browser_cookies", saved)

    assert await _session(page, auth_file=None).keepalive_cookies() is True
    assert len(page.calls) == 1
    saved.assert_not_awaited()
