"""保活轮转：活跃账号之外，每轮再带一个账号做 HTTP 刷新。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aistudio_api.application import cookie_keepalive as kp


def _account(account_id: str, *, enabled: bool = True):
    return SimpleNamespace(id=account_id, enabled=enabled)


def _write_auth(path, cookies):
    path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_pick_rotation_target_skips_active_account():
    accounts = [_account("a"), _account("b"), _account("c")]

    target, cursor = kp.pick_rotation_target(accounts, "a", 0)
    assert target.id == "b"
    assert cursor == 1

    target, cursor = kp.pick_rotation_target(accounts, "a", cursor)
    assert target.id == "c"

    target, cursor = kp.pick_rotation_target(accounts, "a", cursor)
    assert target.id == "b"  # 轮回到头


def test_pick_rotation_target_skips_disabled_accounts():
    accounts = [_account("a"), _account("b", enabled=False), _account("c")]

    target, _ = kp.pick_rotation_target(accounts, "a", 0)
    assert target.id == "c"


def test_pick_rotation_target_without_candidates():
    assert kp.pick_rotation_target([_account("a")], "a", 5) == (None, 5)
    assert kp.pick_rotation_target([], None, 0) == (None, 0)


@pytest.mark.asyncio
async def test_refresh_updates_existing_cookies_only(tmp_path, monkeypatch):
    auth = tmp_path / "acct1" / "auth.json"
    auth.parent.mkdir()
    _write_auth(
        auth,
        [
            {"name": "__Secure-1PSIDTS", "value": "old", "domain": ".google.com"},
            {"name": "SID", "value": "sid-1", "domain": ".google.com"},
        ],
    )

    def fake_refresh(pairs):
        assert pairs["__Secure-1PSIDTS"] == "old"
        return {"__Secure-1PSIDTS": "new", "SID": "sid-1", "__Host-GAPS": "nope"}

    monkeypatch.setattr(kp, "_refresh_session_cookies", fake_refresh)

    changed = await kp.refresh_account_cookies_via_http(auth)

    assert changed == 1
    saved = json.loads(auth.read_text(encoding="utf-8"))
    values = {c["name"]: c["value"] for c in saved["cookies"]}
    assert values["__Secure-1PSIDTS"] == "new"
    assert values["SID"] == "sid-1"
    assert "__Host-GAPS" not in values  # 不认识的条目绝不回写
    assert saved["origins"] == []


@pytest.mark.asyncio
async def test_refresh_noop_when_nothing_changed(tmp_path, monkeypatch):
    auth = tmp_path / "acct1" / "auth.json"
    auth.parent.mkdir()
    _write_auth(auth, [{"name": "SID", "value": "same"}])
    before = auth.stat().st_mtime_ns

    monkeypatch.setattr(kp, "_refresh_session_cookies", lambda pairs: dict(pairs))

    assert await kp.refresh_account_cookies_via_http(auth) == 0
    assert auth.stat().st_mtime_ns == before  # 没变化就不写盘


@pytest.mark.asyncio
async def test_refresh_handles_missing_file_and_empty_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(kp, "_refresh_session_cookies", lambda pairs: dict(pairs))
    assert await kp.refresh_account_cookies_via_http(None) == 0
    assert await kp.refresh_account_cookies_via_http(tmp_path / "nope.json") == 0

    empty = tmp_path / "acct2" / "auth.json"
    empty.parent.mkdir()
    _write_auth(empty, [])
    assert await kp.refresh_account_cookies_via_http(empty) == 0


@pytest.mark.asyncio
async def test_refresh_survives_refresher_error(tmp_path, monkeypatch):
    auth = tmp_path / "acct1" / "auth.json"
    auth.parent.mkdir()
    _write_auth(auth, [{"name": "SID", "value": "sid"}])

    def boom(pairs):
        raise RuntimeError("curl_cffi 挂了")

    monkeypatch.setattr(kp, "_refresh_session_cookies", boom)

    assert await kp.refresh_account_cookies_via_http(auth) == 0
    assert json.loads(auth.read_text(encoding="utf-8"))["cookies"][0]["value"] == "sid"
