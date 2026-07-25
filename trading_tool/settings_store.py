"""
可后台配置项存取（SMTP 等）
==========================
Supabase 模式：复用 service-only 的 `cache` 表（jsonb），key 以 "settings." 前缀区分。
本地回退：SQLite 的 settings 表。
"""

import json

import db
import supabase_client

_PREFIX = "settings."


def get_setting(key: str, default=None):
    if supabase_client.using_supabase():
        row = (supabase_client.get_service_client().table("cache")
               .select("value").eq("key", _PREFIX + key).execute())
        if row.data:
            return row.data[0]["value"].get("v", default)
        return default
    return db.get_setting(key, default)


def set_setting(key: str, value: str) -> None:
    if supabase_client.using_supabase():
        supabase_client.get_service_client().table("cache").upsert({
            "key": _PREFIX + key, "value": {"v": value}, "expires_at": None,
        }).execute()
        return
    db.set_setting(key, value)
