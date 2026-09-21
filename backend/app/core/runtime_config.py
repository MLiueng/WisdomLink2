"""运行时配置缓存（P-06）：sys_config 热路径读取 30s TTL 缓存。

此前每请求读 sys_config 2-3 次（改写 3 轮放大到 6-9 次）；此处进程内
TTL 缓存，写路径（PUT /api/admin/runtime-config）主动失效。
多进程部署时各进程缓存独立，最长 TTL 秒传播延迟（配置项可接受）。

P-02（2026-09-19）：TTL 到期时"旧值先行 + 后台线程续期"（stale-while-revalidate）——
此前到期当次在事件循环内同步开 SessionLocal 读 sys_config（6 个热路径开关每键约
30s 阻塞一次）。现在：缓存命中直接返回；过期时先返回旧值、异步刷新，同步 DB 读取
不再落在请求路径。写路径经 invalidate_runtime 清空缓存，下次读为同步首载（管理面
写操作路径，可接受）；测试直写+失效的口径不受影响。
"""
import threading
import time

_TTL = 30.0
_cache: dict[str, tuple[float, str | None]] = {}
_refreshing: set[str] = set()


def get_runtime(key: str) -> str | None:
    """读 sys_config 值（30s TTL）；行不存在返回 None。

    过期返回旧值并触发后台续期（P-02：事件循环内不做同步 DB 读取）。
    """
    hit = _cache.get(key)
    if hit:
        if time.monotonic() - hit[0] < _TTL:
            return hit[1]
        _refresh_bg(key)
        return hit[1]
    return _load_sync(key)


def _load_sync(key: str) -> str | None:
    """同步读取并回填缓存（首载/写路径失效后第一次读/后台续期共用）。"""
    from app.db import SessionLocal
    from app.models import SysConfig
    val: str | None = None
    db = SessionLocal()
    try:
        row = db.get(SysConfig, key)
        val = row.value if row else None
    except Exception:
        val = None
    finally:
        db.close()
    _cache[key] = (time.monotonic(), val)
    return val


def _refresh_bg(key: str) -> None:
    """后台续期（去重：同键在途只发一次）。"""
    if key in _refreshing:
        return
    _refreshing.add(key)

    def _job():
        try:
            _load_sync(key)
        except Exception:
            pass
        finally:
            _refreshing.discard(key)

    threading.Thread(target=_job, daemon=True, name=f"runtime-refresh-{key}").start()


def invalidate_runtime(*keys: str) -> None:
    """写路径主动失效：指定 key 失效对应项；不给 key 清空全部。"""
    if keys:
        for k in keys:
            _cache.pop(k, None)
    else:
        _cache.clear()
