"""u/N 槽位回退：导入带过来的旧槽位号不能把可用会话判死。"""

from __future__ import annotations

import json

import pytest

from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.gateway import session_auth


def _login_error(auth_user: str) -> SessionExpiredError:
    return SessionExpiredError(
        "Cookie 认证失败：已强制重新注入该账号的凭据，仍被 Google 重定向到登录页。"
        f" (url=https://accounts.google.com/v3/signin/identifier?continue=/u/{auth_user}/)"
    )


@pytest.mark.asyncio
async def test_falls_back_to_u0_when_slot_has_no_session(monkeypatch):
    tried: list[str] = []

    async def fake_once(page, auth_user="0", **kwargs):
        tried.append(auth_user)
        if auth_user != "0":
            raise _login_error(auth_user)

    monkeypatch.setattr(session_auth, "_navigate_once", fake_once)
    resolved: list[str] = []

    await session_auth.navigate_to_aistudio(
        object(),
        auth_user="5",
        auth_file=None,
        on_auth_user_resolved=resolved.append,
    )

    assert tried == ["5", "0"]
    assert resolved == ["0"]


@pytest.mark.asyncio
async def test_slot_zero_failure_is_not_retried(monkeypatch):
    tried: list[str] = []

    async def fake_once(page, auth_user="0", **kwargs):
        tried.append(auth_user)
        raise _login_error(auth_user)

    monkeypatch.setattr(session_auth, "_navigate_once", fake_once)

    with pytest.raises(SessionExpiredError):
        await session_auth.navigate_to_aistudio(object(), auth_user="0")

    assert tried == ["0"]


@pytest.mark.asyncio
async def test_other_errors_are_not_masked_by_the_fallback(monkeypatch):
    tried: list[str] = []

    async def fake_once(page, auth_user="0", **kwargs):
        tried.append(auth_user)
        raise SessionExpiredError("Cookie 认证失败，等待页面就绪期间重定向到 Google 登录页。")

    monkeypatch.setattr(session_auth, "_navigate_once", fake_once)

    with pytest.raises(SessionExpiredError):
        await session_auth.navigate_to_aistudio(object(), auth_user="3")

    assert tried == ["3"]  # 没有第二条 "已强制重新注入仍是登录页" 的提示，不触发回退


def test_set_auth_user_in_meta_updates_meta_json(tmp_path):
    account = tmp_path / "acc"
    account.mkdir()
    auth_file = account / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    meta_path = account / "meta.json"
    meta_path.write_text(
        json.dumps({"auth_user": "5", "email": "a@b.c"}), encoding="utf-8"
    )

    session_auth.set_auth_user_in_meta(str(auth_file), "0")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["auth_user"] == "0"
    assert meta["email"] == "a@b.c"  # 其它字段不能丢


def test_set_auth_user_in_meta_creates_missing_file(tmp_path):
    account = tmp_path / "acc"
    account.mkdir()
    auth_file = account / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")

    session_auth.set_auth_user_in_meta(str(auth_file), "0")

    assert json.loads((account / "meta.json").read_text(encoding="utf-8"))["auth_user"] == "0"
