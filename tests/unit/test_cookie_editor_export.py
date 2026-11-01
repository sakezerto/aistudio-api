"""Cookie-Editor 导出格式：字段、sameSite 映射、hostOnly 与 session 判定。"""

from __future__ import annotations

import json

from aistudio_api.infrastructure.account.cookie_parser import (
    parse_cookie_string,
    to_cookie_editor_json,
)


def _jar() -> list[dict[str, object]]:
    return [
        {
            "name": "__Secure-1PSID",
            "value": "sid-value",
            "domain": ".google.com",
            "path": "/",
            "expires": 1793500000.5,
            "httpOnly": True,
            "secure": True,
            "session": False,
            "sameSite": "None",
        },
        {
            "name": "SID",
            "value": "host-sid",
            "domain": "aistudio.google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "session": True,
            "sameSite": "Lax",
        },
        {
            "name": "ACCOUNT_CHOOSER",
            "value": "chooser",
            "domain": ".google.com",
            "path": "/",
            "expires": 1793500000.0,
            "httpOnly": True,
            "secure": True,
            "session": False,
            "sameSite": "Strict",
        },
    ]


def test_export_matches_cookie_editor_shape():
    export = to_cookie_editor_json(_jar())

    assert len(export) == 3
    assert set(export[0]) == {
        "domain",
        "expirationDate",
        "hostOnly",
        "httpOnly",
        "name",
        "path",
        "sameSite",
        "secure",
        "session",
        "storeId",
        "value",
    }


def test_export_maps_same_site_and_domain_flags():
    export = {item["name"]: item for item in to_cookie_editor_json(_jar())}

    # Google 的 None 在 Cookie-Editor 里写作 no_restriction
    assert export["__Secure-1PSID"]["sameSite"] == "no_restriction"
    assert export["SID"]["sameSite"] == "lax"
    assert export["ACCOUNT_CHOOSER"]["sameSite"] == "strict"

    # 带点的域 → hostOnly=False
    assert export["__Secure-1PSID"]["hostOnly"] is False
    assert export["SID"]["hostOnly"] is True


def test_export_marks_session_cookies_without_expiration_date():
    export = {item["name"]: item for item in to_cookie_editor_json(_jar())}

    assert export["SID"]["session"] is True
    assert "expirationDate" not in export["SID"]

    assert export["__Secure-1PSID"]["session"] is False
    assert export["__Secure-1PSID"]["expirationDate"] == 1793500000.5


def test_export_round_trips_through_the_import_parser():
    """导出的 JSON 原样粘回导入框，必须解析出同一批 cookie。"""
    export = to_cookie_editor_json(_jar())

    storage_state = parse_cookie_string(json.dumps(export))

    names = {c["name"] for c in storage_state["cookies"]}
    assert {"__Secure-1PSID", "SID", "ACCOUNT_CHOOSER"} <= names
    assert storage_state["origins"] == []


def test_export_is_plain_json_serialisable():
    text = json.dumps(to_cookie_editor_json(_jar()), ensure_ascii=False, indent=2)
    assert text.startswith("[")
    assert json.loads(text)[0]["storeId"] == "0"
