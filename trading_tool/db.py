"""
SQLite 数据库层
================
负责连接管理与建表。存放三类数据：
  - users / sessions      ：用户账号、邮箱验证状态、登录会话
  - user_watchlist        ：每个用户自选看板的股票代码
  - daily_data            ：每个标的按「天」粒度的行情（用于回测 / 指标分析）

连接策略：低并发场景（个人工具），使用单条共享连接 + 全局锁，
避免多线程下 sqlite 的非线程安全问题。文件路径由 DATABASE_PATH
环境变量控制，默认落在当前目录 autopilot.db。
"""

import os
import sqlite3
import threading

# 数据库文件路径：本地默认 ./autopilot.db；Render 上可用环境变量指向持久盘
DB_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "autopilot.db"),
)

# 全局连接 + 锁（sqlite 单连接多线程需串行化）
_conn = None
_db_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    """返回（惰性创建）共享连接，row_factory 设为 Row 方便按列名取数。"""
    global _conn
    with _db_lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
        return _conn


def db_lock() -> threading.Lock:
    """暴露全局锁，供各模块在读写时加锁，保证串行化。"""
    return _db_lock


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    verified      INTEGER NOT NULL DEFAULT 0,
    verify_code   TEXT,
    verify_exp    INTEGER,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_watchlist (
    user_id    INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, symbol),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS daily_data (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    source     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    email      TEXT NOT NULL,
    country    TEXT,
    message    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    reply      TEXT,
    created_at TEXT NOT NULL,
    replied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_symbol ON daily_data(symbol);
CREATE INDEX IF NOT EXISTS idx_watch_user   ON user_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_ticket_ct   ON tickets(created_at);
"""


def get_setting(key: str, default: str = None) -> "str | None":
    """读取一条 key-value 设置（用于 SMTP 等可后台配置项）。"""
    conn = get_conn()
    with _db_lock:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """写入/更新一条 key-value 设置。"""
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def _migrate() -> None:
    """对已有库做向后兼容迁移（新列/新表补齐）。"""
    conn = get_conn()
    with _db_lock:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def init_db() -> None:
    """建表（幂等）+ 迁移。服务启动时调用一次。"""
    conn = get_conn()
    with _db_lock:
        conn.executescript(SCHEMA)
        conn.commit()
    _migrate()


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
