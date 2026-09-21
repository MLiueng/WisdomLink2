"""审计（NFR-218 只增）与 Token 计量（BR-012 唯一口径，全调用必经）。

v1.2 调整（用户裁决 2026-09-12）：统计仅呈现 Token 用量/厂商/模型/用途，不展示价格；
费用仅在库内留档（cost 列，配置了单价才计算），不对 API/前端暴露。

P-10 批处理（2026-09-19）：写事务出热路径——
  - `record_usage`/`audit` 只做内存入队（O(1)，零 DB 会话），配额计数随入队即时累加；
  - 后台刷盘线程按「间隔（默认 1s）或批量阈值（默认 50 条）」触发，单会话单事务批写；
  - 用量按 (日, 模型, 库, 用途) 真聚合：同键存在则 UPDATE 累加（含 call_count），
    否则插入（修复"注释称按日聚合、实际逐次插新行"的口径失真）；
  - 刷盘失败逐批重试一次，仍失败按 S-09 强制日志（审计只增可信，不得静默吞掉）；
  - 关停钩子 `stop_flusher` 排空残留（lifespan 接线），管理面读取前 `flush_now` 保证可见。
多进程部署注意：各进程缓冲独立，聚合 UPDATE 存在跨进程并发窗口（秒级，统计口径可接受）。
崩溃丢批边界（已知取舍）：进程异常终止时缓冲内最后一批（≤1s 或 ≤50 条）计量/审计
永久丢失；正常关停经 stop_flusher 无损排空。配额预检为防刷量口径而非计费，可接受。
"""
import json
import os
import threading
import time
from collections import deque
from datetime import date, datetime
from urllib.parse import urlparse
from app.db import SessionLocal
from app.models import AuditLog, ModelUsage
from app.config import get_settings
from app.core.logging_config import setup_logging

_log = setup_logging()

_VENDOR_MAP = {
    "api.deepseek.com": "DeepSeek",
    "open.bigmodel.cn": "智谱",
    "api.openai.com": "OpenAI",
    "api.siliconflow.cn": "SiliconFlow",
    "dashscope.aliyuncs.com": "阿里云百炼",
    "api.jina.ai": "Jina",
    "api.cohere.com": "Cohere",
}

# P-10 批处理参数（间隔可用环境变量收紧，测试环境取小值保证读取可见性）
_FLUSH_INTERVAL = max(0.05, float(os.environ.get("WL2_METER_FLUSH_MS", "1000")) / 1000)
_FLUSH_BATCH = 50            # 批量阈值：入队达到即触发即时刷盘（不等定时器）
_BUFFER_MAX = 20000          # 缓冲上限：防 DB 长时间故障导致内存膨胀（超限丢弃最旧并告警）

_buffer: deque = deque(maxlen=_BUFFER_MAX)
_buf_lock = threading.Lock()
_flush_lock = threading.Lock()   # 刷盘串行化：定时器/阈值触发/显式 flush 互斥
_flusher: threading.Thread | None = None
_stop = threading.Event()
_dropped = 0                     # 缓冲溢出丢弃计数（运营可见）


def vendor_from_url(base_url: str) -> str:
    """从 Provider base_url 自动识别厂商（显式配置优先，本函数仅兜底）：
    本地地址 → 'local'，已知云厂商 → 友好名，未知 → 域名本身（自部署可经配置自命名）。"""
    host = (urlparse(base_url or "").netloc or "").lower().split(":")[0]
    if not host:
        return "unknown"
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return "local"
    return _VENDOR_MAP.get(host, host)


def _enqueue(item: dict) -> None:
    global _dropped
    with _buf_lock:
        if len(_buffer) >= _BUFFER_MAX:
            _dropped += 1
            if _dropped == 1 or _dropped % 1000 == 0:
                _log.error("计量/审计缓冲溢出，丢弃最旧记录 | dropped=%s", _dropped)
        _buffer.append(item)
        need_flush = len(_buffer) >= _FLUSH_BATCH
    if need_flush:
        threading.Thread(target=_flush_once, daemon=True).start()


def audit(action: str, object_type: str = "", object_id: str = "",
          actor: str = "anonymous", detail: dict | None = None) -> None:
    """审计入队（P-10：零 DB 会话，刷盘线程批写；失败可见性由刷盘层保证）。"""
    _enqueue({"kind": "audit", "action": action, "object_type": object_type,
              "object_id": str(object_id), "actor": actor,
              "detail": json.dumps(detail or {}, ensure_ascii=False),
              "at": datetime.now()})


def _price(model_id: str, pt: int, ct: int) -> float:
    prices = get_settings().prices.get(model_id)
    if not prices:
        return 0.0
    return round((pt * prices[0] + ct * prices[1]) / 1_000_000, 2)


def record_usage(model_id: str, provider: str, purpose: str, kb_id: int | None,
                 prompt_tokens: int, completion_tokens: int = 0,
                 is_estimated: bool = False, error: bool = False,
                 cached_tokens: int = 0) -> None:
    """BR-012：usage 原值优先，估算须标注；按自然日聚合落库（P-10：入队批写）。

    cached_tokens（M5）：Prompt Caching 命中的输入 token，计入 prompt_tokens 之内，
    单独留档供"缓存命中率/节省量"看板统计。
    """
    # S-05：配额计数随入队即时累加（不等刷盘，预检口径不失真）
    from app.core.quota import quota_add
    quota_add(prompt_tokens + completion_tokens)
    _enqueue({"kind": "usage", "stat_date": date.today(), "model_id": model_id,
              "provider": provider, "kb_id": kb_id, "purpose": purpose,
              "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
              "error": error, "is_estimated": is_estimated, "cached_tokens": cached_tokens})


def _drain() -> list[dict]:
    with _buf_lock:
        items = list(_buffer)
        _buffer.clear()
    return items


def _write_batch(items: list[dict]) -> None:
    """单会话单事务批写：审计逐条只增；用量按 (日,模型,库,用途) 聚合累加。

    A-1（2026-09-19）：聚合写入采用「UPDATE 原子增量先行 → rowcount 回落插入」，
    替代旧「先 SELECT 再 UPDATE/INSERT」——后者在多进程并发下存在读改写窗口，
    两个进程同时读到旧值各自累加会互相覆盖丢增量。原子 UPDATE 由数据库行级
    排他保证增量不丢；rowcount=0 说明首条记录尚不存在才插入。每批增量恒正
    （call_count≥1），UPDATE 命中时必然产生变更，rowcount 判定不会误判。
    """
    from sqlalchemy import func, update
    db = SessionLocal()
    try:
        agg: dict[tuple, dict] = {}
        audits: list[dict] = []
        for it in items:
            if it["kind"] == "audit":
                audits.append(it)
                continue
            key = (it["stat_date"], it["model_id"], it["kb_id"], it["purpose"])
            slot = agg.setdefault(key, {"provider": it["provider"], "prompt": 0, "completion": 0,
                                        "calls": 0, "errors": 0, "cached": 0, "cost": 0.0,
                                        "estimated": False})
            slot["prompt"] += it["prompt_tokens"]
            slot["completion"] += it["completion_tokens"]
            slot["calls"] += 1
            slot["errors"] += 1 if it["error"] else 0
            slot["cached"] += it["cached_tokens"]
            slot["cost"] += _price(it["model_id"], it["prompt_tokens"], it["completion_tokens"])
            slot["estimated"] = slot["estimated"] or it["is_estimated"]
        for a in audits:
            db.add(AuditLog(actor=a["actor"], action=a["action"], object_type=a["object_type"],
                            object_id=a["object_id"], detail=a["detail"]))
        for (d, model_id, kb_id, purpose), v in agg.items():
            stmt = update(ModelUsage).where(
                ModelUsage.stat_date == d,
                ModelUsage.model_id == model_id,
                ModelUsage.kb_id == kb_id,
                ModelUsage.purpose == purpose,
            ).values(
                prompt_tokens=ModelUsage.prompt_tokens + v["prompt"],
                completion_tokens=ModelUsage.completion_tokens + v["completion"],
                call_count=ModelUsage.call_count + v["calls"],
                error_count=ModelUsage.error_count + v["errors"],
                cached_tokens=ModelUsage.cached_tokens + v["cached"],
                cost=func.round(ModelUsage.cost + v["cost"], 2),
                is_estimated=ModelUsage.is_estimated | v["estimated"],
            )
            cnt = db.execute(stmt).rowcount
            if cnt == 0:
                db.add(ModelUsage(stat_date=d, model_id=model_id, provider=v["provider"], kb_id=kb_id,
                                  purpose=purpose, prompt_tokens=v["prompt"],
                                  completion_tokens=v["completion"], call_count=v["calls"],
                                  error_count=v["errors"], is_estimated=v["estimated"],
                                  cost=v["cost"], cached_tokens=v["cached"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _flush_once(blocking: bool = False) -> None:
    """刷盘一批（串行化）：空缓冲直接返回；失败重试一次，仍失败强制日志。

    blocking（flush_now 专用）：有刷盘在途时等待其完成再排空——非阻塞跳过会
    漏掉该刷盘窗口内新入队的条目，违背"显式刷盘后缓冲必可见"契约。
    """
    if not _flush_lock.acquire(blocking=blocking):
        return   # 已有刷盘在途，避免重复事务
    try:
        items = _drain()
        if not items:
            return
        try:
            _write_batch(items)
        except Exception:
            _log.exception("计量/审计批写失败，重试一次 | batch=%s", len(items))
            try:
                _write_batch(items)
            except Exception:
                # S-09：只增可信前提——写入失败不得静默吞掉，必须带上下文可见
                _log.exception("计量/审计批写重试仍失败（数据丢失风险）| batch=%s | first=%s",
                               len(items), items[0] if items else None)
    finally:
        _flush_lock.release()


def flush_now() -> None:
    """显式刷盘（管理面读取前/测试断言前调用，阻塞排空保证缓冲全量可见）。"""
    _flush_once(blocking=True)


def pending_count() -> int:
    """缓冲中待刷盘条数（运营观测）。"""
    return len(_buffer)


def _loop() -> None:
    while not _stop.wait(_FLUSH_INTERVAL):
        _flush_once()
        _sync_quota()
    _flush_once()   # 关停前排空残留
    _sync_quota()


def _sync_quota() -> None:
    """P-02：配额内存增量批量同步 Redis（quota_add 已改为纯内存累加）。"""
    try:
        from app.core.quota import sync_pending
        sync_pending()
    except Exception:
        _log.exception("配额增量同步失败（下一轮重试）")


def start_flusher() -> None:
    """lifespan 启动钩子（幂等）。"""
    global _flusher
    if _flusher and _flusher.is_alive():
        return
    _stop.clear()
    _flusher = threading.Thread(target=_loop, daemon=True, name="metering-flusher")
    _flusher.start()


def stop_flusher(timeout: float = 5.0) -> None:
    """lifespan 关停钩子：停线程并排空残留（幂等）。"""
    global _flusher
    _stop.set()
    if _flusher:
        _flusher.join(timeout)
        _flusher = None
    _flush_once(blocking=True)   # 关停排空用阻塞口径：与在途刷盘竞争时等待后补排
    _sync_quota()
