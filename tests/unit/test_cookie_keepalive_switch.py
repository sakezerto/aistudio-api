"""切换即保活：账号一切换就立刻补一轮，不等满间隔。"""

from __future__ import annotations

import asyncio
import time

import pytest

from aistudio_api.application import cookie_keepalive as kp
from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session import BrowserSession


@pytest.fixture(autouse=True)
def _reset_signal():
    kp._switch_state["pending"] = False
    event = kp._switch_state.get("event")
    if event is not None:
        event.clear()  # type: ignore[union-attr]
    yield
    kp._switch_state["pending"] = False
    kp._switch_state["loop"] = None
    kp._switch_state["event"] = None


@pytest.mark.asyncio
async def test_notify_wakes_the_loop_immediately():
    asyncio.get_running_loop().call_later(0.05, kp.notify_account_switched, "acc.json")

    started = time.monotonic()
    woken = await kp.wait_for_next_round(30)
    elapsed = time.monotonic() - started

    assert woken is True
    assert elapsed < 1.0  # 没等到 30 秒


@pytest.mark.asyncio
async def test_timeout_returns_false_and_clears():
    assert await kp.wait_for_next_round(0.05) is False
    assert kp._switch_state["pending"] is False


@pytest.mark.asyncio
async def test_signal_is_consumed_once():
    kp.notify_account_switched("acc.json")

    assert await kp.wait_for_next_round(0.05) is True
    # 第二轮必须重新等，不能连着空转
    assert await kp.wait_for_next_round(0.05) is False


@pytest.mark.asyncio
async def test_switch_auth_notifies_keepalive(monkeypatch):
    sess = BrowserSession.__new__(BrowserSession)
    calls: list[str] = []

    async def fake_transition(auth_file, force_refresh=False):
        return None

    monkeypatch.setattr(sess, "transition_to_account", fake_transition)
    monkeypatch.setattr(session_module, "notify_account_switched", calls.append)

    await BrowserSession.switch_auth(sess, "C:/data/accounts/acct-b/auth.json")

    assert calls == ["C:/data/accounts/acct-b/auth.json"]
