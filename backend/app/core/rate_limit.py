"""限流（S-06）：固定窗口计数器，Redis 原子自增优先、进程内存兜底。

- 登录：每 IP 每分钟 rate_login_per_min 次（防暴力破解）
- 问答：每 IP 每分钟 rate_chat_per_min 次（防免登录刷量）
超限返回 429；计数窗口按分钟对齐，窗口翻转自动清零。
"""
import time
from fastapi import HTTPException, Request
from app.config import get_settings

# 内存兜底：key -> (count, window_start_monotonic)
_mem: dict[str, tuple[int, float]] = {}


def _redis():
    try:
        from app.core.cache import _get_client
        return _get_client()
    except Exception:
        return None


def hit(key: str, limit: int, window: int = 60) -> bool:
    """计数 +1；未超限返回 True，超限返回 False。limit<=0 视为不限流。"""
    if limit <= 0:
        return True
    c = _redis()
    if c:
        try:
            rkey = f"wl2:rl:{key}"
            n = c.incr(rkey)
            if n == 1:
                c.expire(rkey, window)
            return n <= limit
        except Exception:
            pass
    now = time.monotonic()
    cnt, start = _mem.get(key, (0, now))
    if now - start >= window:
        cnt, start = 0, now
    cnt += 1
    _mem[key] = (cnt, start)
    return cnt <= limit


def reset() -> None:
    """清空内存计数（测试/排障用）。"""
    _mem.clear()


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_login_rate(request: Request) -> None:
    """登录限流依赖：超限 429，并写审计留痕（暴力破解取证）。"""
    ip = _ip(request)
    if not hit(f"login:{ip}", get_settings().rate_login_per_min, 60):
        from app.core.metering import audit
        audit("admin.login_rate_limited", "ip", ip)
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")


def require_chat_rate(request: Request) -> None:
    """问答限流依赖：免登录入口按 IP 限流，超限 429。"""
    ip = _ip(request)
    if not hit(f"chat:{ip}", get_settings().rate_chat_per_min, 60):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
