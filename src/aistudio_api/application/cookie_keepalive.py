"""空闲保活的账号轮转：活跃账号走浏览器，另一个账号走 HTTP。

浏览器级的同源请求最可靠（能拿到 Set-Cookie 并让 1PSIDTS 真正滚动），但只有
当前激活的那个账号有页面。为了不让其余账号在闲置期慢慢烂掉，每轮再挑一个
账号做一次 HTTP 级刷新（curl_cffi 走 Google 登录流），代价很小。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
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
