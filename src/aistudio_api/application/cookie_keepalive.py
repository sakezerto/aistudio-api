"""空闲保活：每轮给活跃账号 + 下一个账号各做一次浏览器级保活。

活跃账号复用主浏览器页面做同源请求；下一个账号用**它自己的 profile** 起一个短命
浏览器，模拟登录后把全量 cookie 覆盖回 auth.json。HTTP（curl_cffi）只作兜底。
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from aistudio_api.infrastructure.account.cookie_refresher import (
    _refresh_session_cookies,
)
from aistudio_api.infrastructure.utils.common import atomic_write_json
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("keepalive")


class _HasAccountFields(Protocol):
    id: str
    enabled: bool


_switch_state: dict[str, object] = {"loop": None, "event": None, "pending": False}


def _signal() -> asyncio.Event:
    """取当前事件循环上的唤醒事件；跨循环（及测试）时重建，pending 会带过去。"""
    loop = asyncio.get_running_loop()
    if _switch_state["loop"] is not loop or _switch_state["event"] is None:
        event = asyncio.Event()
        if _switch_state["pending"]:
            event.set()
        _switch_state["loop"] = loop
        _switch_state["event"] = event
    return _switch_state["event"]  # type: ignore[return-value]


def notify_account_switched(auth_file: str | None = None) -> None:
    """账号切换后唤醒保活循环：不用等满一个间隔就补一轮。"""
    _switch_state["pending"] = True
    # 没有运行中的事件循环时（极少见）只留 pending 标记，下一轮照样生效
    with suppress(RuntimeError):
        _signal().set()
    log.debug("保活收到账号切换信号: %s", auth_file)


async def wait_for_next_round(interval: float) -> bool:
    """等下一轮；返回 True 表示被"账号切换"提前唤醒。"""
    event = _signal()
    if _switch_state["pending"]:
        _switch_state["pending"] = False
        event.clear()
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=interval)
    except TimeoutError:
        return False
    finally:
        _switch_state["pending"] = False
        event.clear()
    return True


def pick_rotation_target(
    accounts: Sequence[_HasAccountFields],
    active_id: str | None,
    cursor: int,
) -> tuple[_HasAccountFields | None, int]:
    """按轮转顺序挑一个"下一个"账号（排除当前激活的）。

    返回 (账号 or None, 新游标)。cursor 用绝对计数，账号增删后仍然单调推进。
    """
    candidates = [a for a in accounts if getattr(a, "enabled", True) and a.id != active_id]
    if not candidates:
        return None, cursor
    index = cursor % len(candidates)
    return candidates[index], cursor + 1


def _cookie_pairs(cookies: Iterable[dict[str, object]]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if not name:
            continue
        value = cookie.get("value")
        pairs[str(name)] = "" if value is None else str(value)
    return pairs


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load_auth_cookies(path: Path) -> list[dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cookies = data.get("cookies") or []
    return [c for c in cookies if isinstance(c, dict) and c.get("name")]


async def keepalive_account_in_browser(
    auth_path: Path | str | None,
    *,
    headless: bool = True,
) -> bool:
    """起一个短命浏览器模拟登录，再把它的全量 cookie 覆盖回 auth.json。

    为什么必须用浏览器：``__Secure-1PSIDTS`` 受 DBSC 绑定，裸 HTTP（curl_cffi）拿不到
    新的 Set-Cookie，Google 只会把请求踢回登录页。这里用同一个 CloakBrowser 二进制 +
    该账号自己的 profile，TLS 指纹、设备密钥、DBSC 绑定都和正常使用时一致。

    流程：注入现有 cookie → 走 accounts.google.com 登录引导 → 打开 AI Studio →
    抓浏览器里所有域的 cookie → **整份覆盖** auth.json（不是增量合并）。

    返回 True 表示登录成功且已全量覆盖；停在登录页、抓不到 cookie 都算失败，失败不写盘。
    """
    path = Path(auth_path) if auth_path else None
    if path is None or not path.is_file():
        return False
    cookies = _load_auth_cookies(path)
    if not cookies:
        return False

    from aistudio_api.infrastructure.browser.browser_engine import (
        launch_chromium_process,
    )
    from aistudio_api.infrastructure.browser.cdp_client import CDPClient
    from aistudio_api.infrastructure.gateway.session_auth import (
        bootstrap_google_session,
        save_browser_cookies,
    )

    account = path.parent.name
    profile = path.parent / "profile"
    if (profile / "SingletonLock").exists():
        log.info("账号 %s 的 profile 正被主浏览器占用，本轮跳过", account)
        return False

    port = _pick_free_port()
    proc = None
    client = None
    page = None
    try:
        proc = await asyncio.to_thread(
            launch_chromium_process, port, str(profile), headless=headless
        )
        client = CDPClient(port)
        await client.wait_until_ready(timeout_s=25.0)
        page = await client.create_new_page(block_assets=True)
        await page.set_cookies(cookies)

        # 模拟一次真实登录：accounts.google.com 引导 → AI Studio
        await bootstrap_google_session(page)
        current_url = page.url or ""
        if "accounts.google.com" in current_url or "ServiceLogin" in current_url:
            log.warning("账号 %s 保活失败: 登录流程仍停在 Google 登录页", account)
            return False

        fresh = await page.get_cookies()
        if not fresh:
            log.warning("账号 %s 保活失败: 浏览器没有返回任何 cookie", account)
            return False

        await save_browser_cookies(page, str(path), fresh, replace=True)
        log.info(
            "账号 %s 保活完成: 浏览器登录成功，已全量覆盖 %d 条 cookie", account, len(fresh)
        )
        return True
    except Exception as e:
        log.warning("账号 %s 浏览器级保活异常: %s", account, e)
        return False
    finally:
        with suppress(Exception):
            if page is not None and not page.is_closed():
                await page.close()
        with suppress(Exception):
            if client is not None:
                await client.close()
        with suppress(Exception):
            if proc is not None:
                await asyncio.to_thread(proc.terminate, 3.0)


async def refresh_account_cookies_via_http(auth_path: Path | str | None) -> int:
    """对非活跃账号做一次 HTTP 级 cookie 刷新，返回实际变化的条目数。

    只更新 auth.json 里已存在的条目——HTTP 侧拿不到浏览器 profile 里的东西，
    贸然合并新名字反而可能把 __Host-* 之类的坏数据写回去。
    """
    path = Path(auth_path) if auth_path else None
    if path is None or not path.is_file():
        return 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("保活读取 %s 失败: %s", path, e)
        return 0

    cookies = data.get("cookies") or []
    pairs = _cookie_pairs(cookies)
    if not pairs:
        return 0

    try:
        # curl_cffi 是同步阻塞调用，扔到线程里别卡事件循环
        refreshed = await asyncio.to_thread(_refresh_session_cookies, pairs)
    except Exception as e:
        log.debug("HTTP 保活失败 (%s): %s", path.parent.name, e)
        return 0

    changed = 0
    for cookie in cookies:
        name = cookie.get("name")
        if not name:
            continue
        new_value = refreshed.get(str(name))
        if new_value is None or new_value == cookie.get("value"):
            continue
        cookie["value"] = new_value
        changed += 1

    if changed:
        try:
            atomic_write_json(
                path,
                {"cookies": cookies, "origins": data.get("origins") or []},
            )
        except OSError as e:
            log.debug("保活回写 %s 失败: %s", path, e)
            return 0
        log.info("账号 %s HTTP 保活: 刷新 %d 条 cookie", path.parent.name, changed)
    return changed
