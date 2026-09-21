"""数据库会话管理。开发环境未配置 MySQL 时自动回退 SQLite（data/wl2.db）。

P-13：同步引擎（管理面/入库等既有路径）+ 异步引擎（问答热路径读事务）并存；
两者连接池均显式配置（原默认 5+10 撑不住每请求 10+ 次会话开合）。
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _db_url() -> str:
    url = get_settings().db_url
    if url.startswith("sqlite"):
        Path("./data").mkdir(exist_ok=True)
    return url


def _engine_kwargs() -> dict:
    """按驱动装配引擎：SQLite 开 WAL + 锁等待超时；MySQL 加连接超时（探针/启动不被拖死）。"""
    url = get_settings().db_url
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 15},  # busy_timeout：并发写锁等待上限 15s
                "pool_pre_ping": True, "pool_recycle": 3600}
    return {"connect_args": {"connect_timeout": 5},  # MySQL：连接建立上限 5s
            "pool_pre_ping": True, "pool_recycle": 3600,
            # P-13：显式连接池（默认 5+10 不足以支撑并发会话开合）
            "pool_size": 10, "max_overflow": 20}


engine = create_engine(_db_url(), **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _async_db_url() -> str:
    """同步 URL → 异步驱动 URL（sqlite+aiosqlite / mysql+aiomysql）。"""
    url = _db_url()
    if url.startswith("sqlite"):
        return url.replace("sqlite", "sqlite+aiosqlite", 1)
    return url.replace("mysql+pymysql", "mysql+aiomysql", 1)


def _async_engine_kwargs() -> dict:
    # NullPool：异步连接随用随建（SQLite/aiomysql 连接均绑定创建时的事件循环，
    # 池化复用跨循环会报错；热路径收益来自非阻塞 IO 而非连接复用）。
    url = get_settings().db_url
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 15}, "poolclass": NullPool}
    return {"connect_args": {"connect_timeout": 5}, "poolclass": NullPool}


from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

async_engine = create_async_engine(_async_db_url(), **_async_engine_kwargs())
AsyncSessionLocal = async_sessionmaker(bind=async_engine, autoflush=False, expire_on_commit=False)

if get_settings().db_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _rec):  # WAL：读写并发，避免入库/问答互锁（审计 M4）
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    @event.listens_for(async_engine.sync_engine, "connect")
    def _sqlite_pragma_async(dbapi_conn, _rec):  # 异步连接同开 WAL
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db():
    """P-13：热路径异步会话依赖（AsyncSession），读写不阻塞事件循环。"""
    db = AsyncSessionLocal()
    try:
        yield db
    finally:
        await db.close()
