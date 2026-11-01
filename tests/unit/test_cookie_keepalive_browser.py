"""浏览器级保活：走账号自己的 profile，而不是裸 HTTP。"""

from __future__ import annotations

import json

import pytest

from aistudio_api.application import cookie_keepalive as kp
from aistudio_api.infrastructure.browser import browser_engine, cdp_client as cdp_module
from aistudio_api.infrastructure.gateway import session_auth


class _Page:
    def __init__(
        self,
        url: str,
        cookies: list[dict[str, object]] | None = None,
        keep_jar: bool = True,
    ):
        self.keep_jar = keep_jar
        self.url = url
        self.closed = False
        self.cookies = cookies if cookies is not None else [{"name": "SID", "value": "x"}]
        self.injected: list[dict[str, object]] | None = None
        self.visited: list[str] = []
        self.fetched = False
        self.cleared = False

    def is_closed(self) -> bool:
        return self.closed

    async def set_cookies(self, cookies):
        # 模拟浏览器 cookie jar：注入即覆盖整份 jar
        self.injected = list(cookies)
        if self.keep_jar:
            self.cookies = list(cookies)

    async def goto(self, url: str, timeout_s: float = 30.0, **kwargs):
        self.visited.append(url)
        return

    async def wait_for_timeout(self, ms: float):
        return None

    async def evaluate(self, expression: str, args=None, **kwargs):
        self.fetched = True
        return 200

    async def clear_cookies(self):
        self.cleared = True
        self.cookies = []

    async def get_cookies(self):
        return self.cookies

    async def close(self):
        self.closed = True


class _Client:
    def __init__(self, page: _Page):
        self.page = page
        self.closed = False

    async def wait_until_ready(self, timeout_s: float = 25.0):
        return None

    async def create_new_page(self, block_assets: bool = True):
        return self.page

    async def close(self):
        self.closed = True


class _Proc:
    def __init__(self):
        self.terminated = False

    def terminate(self, timeout_s: float = 3.0):
        self.terminated = True


def _make_account(tmp_path, cookies=None):
    account_dir = tmp_path / "acct-x"
    account_dir.mkdir()
    auth = account_dir / "auth.json"
    payload = {"cookies": cookies if cookies is not None else [{"name": "SID", "value": "seed"}]}
    auth.write_text(json.dumps(payload), encoding="utf-8")
    return auth


def _patch_browser(monkeypatch, page, proc):
    client = _Client(page)
    monkeypatch.setattr(browser_engine, "launch_chromium_process", lambda *a, **k: proc)
    monkeypatch.setattr(cdp_module, "CDPClient", lambda *a, **k: client)
    saved: list[tuple[object, str, object]] = []

    async def fake_save(p, auth_file, cookies=None, **kwargs):
        saved.append((p, auth_file, cookies, kwargs.get("replace")))

    monkeypatch.setattr(session_auth, "save_browser_cookies", fake_save)
    return client, saved


@pytest.mark.asyncio
async def test_browser_keepalive_success(tmp_path, monkeypatch):
    auth = _make_account(tmp_path)
    page = _Page("https://aistudio.google.com/app/prompts/new_chat")
    proc = _Proc()
    client, saved = _patch_browser(monkeypatch, page, proc)

    assert await kp.keepalive_account_in_browser(auth) is True

    assert page.injected == [{"name": "SID", "value": "seed"}]
    assert page.cleared is True, "没有做回灌自检（清空后只用导出的 cookie 重登）"
    google_hits = [u for u in page.visited if "accounts.google.com" in u]
    assert len(google_hits) >= 2, "自检那一轮没有重新走登录引导"
    assert any("aistudio.google.com" in u for u in page.visited), "没有打开 AI Studio"
    assert len(saved) == 1  # 已回写 auth.json
    assert saved[0][3] is True, "保活必须全量覆盖，而不是增量合并"
    assert client.closed is True
    assert proc.terminated is True


@pytest.mark.asyncio
async def test_browser_keepalive_detects_login_redirect(tmp_path, monkeypatch):
    auth = _make_account(tmp_path)
    page = _Page("https://accounts.google.com/ServiceLogin?continue=...")
    proc = _Proc()
    _client, saved = _patch_browser(monkeypatch, page, proc)

    assert await kp.keepalive_account_in_browser(auth) is False
    assert saved == []  # 绝不能用登录页的 cookie 覆盖 auth.json
    assert proc.terminated is True


@pytest.mark.asyncio
async def test_browser_keepalive_without_cookies(tmp_path, monkeypatch):
    auth = _make_account(tmp_path, cookies=[])
    page = _Page("https://aistudio.google.com/app/prompts/new_chat")
    proc = _Proc()
    monkeypatch.setattr(
        browser_engine, "launch_chromium_process", lambda *a, **k: pytest.fail("不该启动浏览器")
    )
    _client, saved = _patch_browser(monkeypatch, page, proc)

    assert await kp.keepalive_account_in_browser(auth) is False
    assert saved == []


@pytest.mark.asyncio
async def test_browser_keepalive_handles_launch_failure(tmp_path, monkeypatch):
    auth = _make_account(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("没有 CloakBrowser")

    monkeypatch.setattr(browser_engine, "launch_chromium_process", boom)

    assert await kp.keepalive_account_in_browser(auth) is False


@pytest.mark.asyncio
async def test_browser_keepalive_rejects_empty_cookie_jar(tmp_path, monkeypatch):
    auth = _make_account(tmp_path)
    page = _Page(
        "https://aistudio.google.com/app/prompts/new_chat",
        cookies=[],
        keep_jar=False,
    )
    proc = _Proc()
    _client, saved = _patch_browser(monkeypatch, page, proc)

    assert await kp.keepalive_account_in_browser(auth) is False
    assert saved == []


@pytest.mark.asyncio
async def test_browser_keepalive_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser_engine, "launch_chromium_process", lambda *a, **k: pytest.fail("不该启动浏览器")
    )
    assert await kp.keepalive_account_in_browser(tmp_path / "nope" / "auth.json") is False
    assert await kp.keepalive_account_in_browser(None) is False
