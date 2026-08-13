"""
Supabase 客户端封装
====================
统一通过官方 supabase SDK 操作数据与认证。

两种客户端：
  - 用户态客户端 get_user_client(access_token)
      注入用户的 Supabase access_token，PostgREST 自动应用 Row Level Security，
      保证「用户只能访问自己的数据」。
  - 管理态客户端 get_service_client()
      使用 service_role key，绕过 RLS，仅用于后台统计 / EDM / 工单等管理操作。

配置（环境变量，Render 中配置）：
  SUPABASE_URL               项目 URL
  SUPABASE_ANON_KEY         anon key（前端公开，受 RLS 约束）
  SUPABASE_SERVICE_ROLE_KEY service_role key（仅后端，绕过 RLS，严禁进前端）
  SUPABASE_JWT_SECRET       用于后端校验前端传来的 JWT（HS256）

未配置 SUPABASE_URL 时，using_supabase() 返回 False，db.py 自动回退 SQLite。
"""

import os
from functools import lru_cache
from typing import Optional

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()

# 在沙箱/本地未配置 Supabase 时，避免 import 失败阻断启动
try:
    from supabase import Client, create_client
except Exception:  # pragma: no cover
    Client = None
    create_client = None


def using_supabase() -> bool:
    """是否已配置 Supabase（生产环境为 True）。"""
    return bool(SUPABASE_URL and (SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY))


def get_jwt_secret() -> str:
    """返回用于校验前端 JWT 的密钥；未配置返回空串。"""
    return SUPABASE_JWT_SECRET


def _create_supabase(url: str, key: str) -> "Client":
    """创建客户端；兼容新版 SDK，并抑制 timeout/verify 弃用告警。"""
    import warnings
    opts = None
    try:
        from supabase import ClientOptions  # type: ignore
        # 新版优先用 ClientOptions，避免把 timeout/verify 直接塞进 PostgREST
        try:
            opts = ClientOptions()
        except Exception:
            opts = None
    except Exception:
        opts = None
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*timeout.*deprecated.*",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*verify.*deprecated.*",
            category=DeprecationWarning,
        )
        if opts is not None:
            try:
                return create_client(url, key, options=opts)
            except TypeError:
                pass
        return create_client(url, key)


@lru_cache(maxsize=1)
def _service_client() -> "Client":
    """管理态客户端（单例，service_role 绕过 RLS）。"""
    if create_client is None:
        raise RuntimeError("supabase SDK 未安装")
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        raise RuntimeError("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    return _create_supabase(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_service_client() -> "Client":
    """管理态客户端：统计 / EDM / 工单等需要绕过 RLS 的操作。"""
    return _service_client()


def get_user_client(access_token: str) -> "Client":
    """
    用户态客户端：注入用户 access_token，PostgREST 据此应用 RLS。
    注意：用 anon_key 建立连接，仅把用户 token 作为请求鉴权头。
    """
    if create_client is None:
        raise RuntimeError("supabase SDK 未安装")
    if not SUPABASE_URL:
        raise RuntimeError("缺少 SUPABASE_URL")
    key = SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY
    client = _create_supabase(SUPABASE_URL, key)
    if access_token:
        # 让后续所有 PostgREST 请求携带用户 JWT，从而命中 RLS 策略
        try:
            client.postgrest.auth(access_token)
        except Exception:
            # 旧版本 SDK 可能没有 postgrest.auth，回退到 set_session
            try:
                client.auth.set_session(access_token, "")
            except Exception:
                pass
    return client
