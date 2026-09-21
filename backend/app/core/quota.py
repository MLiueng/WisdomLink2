"""日 Token 配额预检（S-05）：入口预检，超限拒答，堵住免登录刷量。

此前 `daily_token_limit` 只在用量看板展示（记账在调用后），免登录问答可无限
刷远程 LLM 账单。现在：
  - 计数：Redis 原子自增（按日 key，午夜过期）；无 Redis 时 DB 当日聚合
    （30s TTL 缓存）+ 进程内增量补偿；
  - 预检：问答入口 `check_quota(estimate)`，当日用量+预估 > 限额 → 友好拒答；
  - 记账：`record_usage` 每次调用后 `quota_add` 累加，与看板口径一致。

P-02（2026-09-19）：`quota_add` 改为纯内存累加（O(1)，零网络/零 DB）——
此前配置 Redis 时每次计量在事件循环内做 2 次同步 Redis 往返（最坏含重连
ping 2s）。现在增量进 `_pending`，由计量刷盘线程 `sync_pending()` 定期
批量同步 Redis；读取端 `quota_used()` 恒叠加 `_pending`，口径不低估。
"""
import threading
import time
from datetime import date, datetime, timedelta
from app.config import get_settings

_lock = threading.Lock()
_pending = 0                                     # 未同步增量：内存累加，读端叠加，不低估
_db_cache: tuple[float, str, int] | None = None  # 无 Redis 时 (monotonic_ts, day, db_sum)
_TTL = 30.0


def _day() -> str:
    return date.today().isoformat()


def _key() -> str:
    return f"wl2:quota:{_day()}"


def _secs_to_midnight() -> int:
    now = datetime.now()
    return max(60, int((datetime(now.year, now.month, now.day) + timedelta(days=1) - now).total_seconds()))


def _redis():
    try:
        from app.core.cache import _get_client
        return _get_client()
    except Exception:
        return None


def _db_sum_today() -> int:
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models import ModelUsage
    db = SessionLocal()
    try:
        return int(db.execute(
            select(func.coalesce(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens), 0))
            .where(ModelUsage.stat_date == date.today())).scalar() or 0)
    finally:
        db.close()


def sync_pending() -> None:
    """把内存增量批量同步 Redis（由计量刷盘线程定期调用；无 Redis 时不动）。

    持锁读出并清零增量，Redis 写入失败则回填——任何路径都不丢计数。
    """
    global _pending
    with _lock:
        if _pending <= 0:
            return
        c = _redis()
        if not c:
            return   # 无 Redis：增量留在内存，读端叠加；恢复后下一轮同步
        delta, _pending = _pending, 0
    try:
        c.incrby(_key(), delta)
        c.expire(_key(), _secs_to_midnight())
    except Exception:
        with _lock:
            _pending += delta   # 写失败回填，等下一轮重试


def quota_used() -> int:
    """今日已用 Token：Redis 计数优先；无 Redis 回退 DB 聚合（30s 缓存）；
    恒叠加未同步增量 `_pending`（Redis 写延迟化后口径不失真）。"""
    global _pending, _db_cache
    c = _redis()
    if c:
        try:
            v = c.get(_key())
            if v is None:
                seed = _db_sum_today()
                c.set(_key(), seed, ex=_secs_to_midnight())
                v = seed
            with _lock:
                return int(v) + _pending
        except Exception:
            pass
    now = time.monotonic()
    refresh = _db_cache is None or _db_cache[1] != _day() or now - _db_cache[0] >= _TTL
    if refresh:
        # P-10：缓存刷新前先排空计量缓冲——否则"已入队未刷盘"的一批在
        # _pending 清零瞬间既不在旧聚合也不在新增量里，产生 ≤30s 低估窗口
        from app.core.metering import flush_now
        flush_now()
    with _lock:
        if refresh:
            _db_cache = (time.monotonic(), _day(), _db_sum_today())
            _pending = 0
        return _db_cache[2] + _pending


def quota_add(tokens: int) -> None:
    """记账累加（record_usage 每次调用后触发）：纯内存累加（P-02 出事件循环），
    与看板"今日 Token"口径一致；Redis 同步由刷盘线程批量完成。"""
    global _pending
    if tokens <= 0:
        return
    with _lock:
        _pending += tokens


def check_quota(estimate_tokens: int) -> tuple[bool, int, int]:
    """入口预检：返回 (是否放行, 已用量, 限额)。限额<=0 视为不启用。"""
    limit = get_settings().daily_token_limit
    if limit <= 0:
        return True, 0, 0
    used = quota_used()
    return (used + max(0, estimate_tokens)) <= limit, used, limit
