"""权益等级探测：失败必须与"确实 Free"区分开。

回归背景：``probe_account_benefit_tier`` 以前在任何失败（超时/非 200/空响应/无 cookie）
时都返回 ``"Free"``，而启动巡检会把这个结果连同 ``tier_checked_at`` 一起写进 registry，
于是一次网络抖动就能把付费账号永久降级成 Free 并锁住 3 天。
"""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.infrastructure.account import cookie_parser


class _Resp:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _Client:
    """最小 httpx.AsyncClient 替身。"""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> _Resp:
        self.requests.append({"url": url, **kwargs})
        if isinstance(self._outcome, Exception):
            raise self._outcome
        assert isinstance(self._outcome, _Resp)
        return self._outcome


def _patch_client(monkeypatch: pytest.MonkeyPatch, outcome: object) -> _Client:
    client = _Client(outcome)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    return client


@pytest.mark.asyncio
async def test_strict_probe_returns_none_on_non_200(monkeypatch):
    _patch_client(monkeypatch, _Resp(401, ""))

    tier = await cookie_parser.probe_account_benefit_tier_strict({"SID": "x"})

    assert tier is None


@pytest.mark.asyncio
async def test_strict_probe_returns_none_on_empty_body(monkeypatch):
    _patch_client(monkeypatch, _Resp(200, "   "))

    tier = await cookie_parser.probe_account_benefit_tier_strict({"SID": "x"})

    assert tier is None


@pytest.mark.asyncio
async def test_strict_probe_returns_none_on_transport_error(monkeypatch):
    _patch_client(monkeypatch, httpx.ConnectTimeout("boom"))

    tier = await cookie_parser.probe_account_benefit_tier_strict({"SID": "x"})

    assert tier is None


@pytest.mark.asyncio
async def test_strict_probe_returns_none_without_cookies(monkeypatch):
    client = _patch_client(monkeypatch, _Resp(200, "[]"))

    tier = await cookie_parser.probe_account_benefit_tier_strict({})

    assert tier is None
    assert client.requests == []


@pytest.mark.asyncio
async def test_legacy_probe_still_falls_back_to_free(monkeypatch):
    _patch_client(monkeypatch, _Resp(500, ""))

    tier = await cookie_parser.probe_account_benefit_tier({"SID": "x"})

    assert tier == "Free"


@pytest.mark.asyncio
async def test_strict_probe_parses_real_tier(monkeypatch):
    _patch_client(monkeypatch, _Resp(200, "[2]"))

    tier = await cookie_parser.probe_account_benefit_tier_strict({"SID": "x"})

    assert tier == "Ultra"


@pytest.mark.asyncio
async def test_strict_probe_keeps_genuine_free(monkeypatch):
    _patch_client(monkeypatch, _Resp(200, "[0]"))

    tier = await cookie_parser.probe_account_benefit_tier_strict({"SID": "x"})

    assert tier == "Free"
