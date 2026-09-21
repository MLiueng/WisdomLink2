"""Redis 缓存：连接失败自动退化为进程内字典（NFR-222 降级口径）。

P-09 修复：① 一次连接失败不再永久放弃（30s 退避后重试，避免 Redis 恢复后
仍长期走内存兜底）；② 内存兜底支持 TTL 惰性过期（此前永不过期，长期运行
存在无界增长风险）；③ 接口签名保持不变，调用方无感知。
"""
import json
import time
from typing import Any
from app.config import get_settings

_memory: dict[str, Any] = {}
# 内存兜底带过期时间：key -> (value, expire_monotonic | None)
_memory_exp: dict[str, float | None] = {}
_client = None
_tried = False
_retry_after = 0.0   # 下次允许重连的 monotonic 时刻（失败退避）
_RETRY_INTERVAL = 30.0


def _get_client():
    """懒连接：失败后按 30s 退避重试，而不是标记永久失败。"""
    global _client, _tried, _retry_after
    if _tried:
        if _client is not None or time.monotonic() < _retry_after:
            return _client
    _tried = True
    _retry_after = time.monotonic() + _RETRY_INTERVAL
    try:
        import redis
        # socket_timeout：读写超时（探针/业务调用不被慢连接拖死，审计 M5）
        _client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2,
                                       socket_timeout=2, decode_responses=True)
        _client.ping()
    except Exception:
        _client = None
    return _client


def reset_client_state() -> None:
    """测试/排障：清空连接状态与内存兜底（恢复"未连接过"初始态）。"""
    global _client, _tried, _retry_after
    _client, _tried, _retry_after = None, False, 0.0
    _memory.clear()
    _memory_exp.clear()


def _mem_get(key: str) -> Any:
    exp = _memory_exp.get(key)
    if exp is not None and time.monotonic() >= exp:
        _memory.pop(key, None)
        _memory_exp.pop(key, None)
        return None
    return _memory.get(key)


def _mem_set(key: str, value: Any, ttl: int | None) -> None:
    _memory[key] = value
    _memory_exp[key] = time.monotonic() + ttl if ttl else None


def cache_get(key: str) -> Any:
    c = _get_client()
    if c:
        try:
            v = c.get(key)
            return json.loads(v) if v is not None else None
        except Exception:
            return _mem_get(key)
    return _mem_get(key)


def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    c = _get_client()
    if c:
        try:
            c.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
            return
        except Exception:
            pass
    _mem_set(key, value, ttl)


def cache_delete(*keys: str) -> None:
    c = _get_client()
    for k in keys:
        _memory.pop(k, None)
        _memory_exp.pop(k, None)
        if c:
            try:
                c.delete(k)
            except Exception:
                pass
