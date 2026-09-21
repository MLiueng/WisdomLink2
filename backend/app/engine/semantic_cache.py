"""语义答案缓存（P-04）：QA → 语义缓存 → RAG 三级命中的第二级。

两级判定：
  L1 归一化精确匹配（同 qa_engine 归一化口径，O(1)）；
  L2 查询向量近邻（复用 BR-011 双阈值——阈值+领先幅度，防语义漂移误命中）。

失效与版本挂钩：缓存记录携带「知识库发布指纹」（发布文档数+当前版本列表哈希）
与发布纪元（KB 发布/删除文档时递增）——文档变更后指纹/纪元变化，旧答案自然不命中，
由陈旧探针兜底验证。

只缓存本地可信结果：联网补充、降级、拒答、多轮上下文（历史参与）一律不缓存。
命中应答零 Token 消耗（usage=0），tokens_saved 与 QA 秒回同口径（BR-012）。
"""
import hashlib
import json
from sqlalchemy import select
from app.config import get_settings
from app.core.rules import normalize_question
from app.db import SessionLocal
from app.models import Document, DocVersion

_PREFIX = "wl2:ans:"
_DEFAULT_TTL = 1800  # 30min：版本指纹失效为主，TTL 兜底


def _norm(v) -> str:
    return normalize_question(v)


def cache_enabled() -> bool:
    # 灰度纪律（规划 §纪律 2）：新功能默认关闭，sys_config 显式开启后生效
    from app.core.runtime_config import get_runtime
    return (get_runtime("runtime_semantic_cache") or "off") == "on"


def kb_epoch(kb_ids: list[int]) -> int:
    """发布纪元：KB 发布/删除文档时递增，旧纪元缓存整体作废。"""
    from app.core.runtime_config import get_runtime
    try:
        return int(get_runtime("cache_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def bump_epoch() -> None:
    from app.core.runtime_config import invalidate_runtime
    from app.models import SysConfig
    db = SessionLocal()
    try:
        row = db.get(SysConfig, "cache_epoch")
        if row:
            try:
                row.value = str(int(row.value) + 1)
            except ValueError:
                row.value = "1"
            row.version = (row.version or 1) + 1
        else:
            db.add(SysConfig(key="cache_epoch", value="1"))
        db.commit()
    finally:
        db.close()
    invalidate_runtime("cache_epoch")


def version_fingerprint(kb_ids: list[int]) -> str:
    """发布指纹：发布文档数 + 当前版本标识集合哈希（任一文档发布/更新即变化）。"""
    db = SessionLocal()
    try:
        docs = db.scalars(select(Document).where(Document.kb_id.in_(sorted(kb_ids)),
                                                 Document.status == "published")).all()
        sig = sorted(f"{d.id}:{d.current_version}:{d.folder_id}" for d in docs)
        return f"{len(docs)}|{hashlib.sha256('|'.join(sig).encode()).hexdigest()[:12]}"
    finally:
        db.close()


def _entry_key(kb_ids: list[int]) -> str:
    return _PREFIX + ",".join(str(k) for k in sorted(kb_ids))


def _load(kb_ids: list[int]) -> dict:
    from app.core.cache import cache_get
    return cache_get(_entry_key(kb_ids)) or {}


def lookup(kb_ids: list[int], question: str,
           qvec: list[float] | None = None) -> dict | None:
    """两级命中判定；返回缓存载荷或 None。qvec 由调用方透传（P-03 复用，避免二次嵌入）。"""
    if not cache_enabled():
        return None
    from app.core.cache import cache_get, cache_set
    cache_set("wl2:ans_lookups", int(cache_get("wl2:ans_lookups") or 0) + 1)
    nq = _norm(question)
    if not nq:
        return None
    entries = _load(kb_ids)
    if not entries:
        return None
    epoch, fp = kb_epoch(kb_ids), version_fingerprint(kb_ids)
    valid = {k: v for k, v in entries.items()
             if v.get("epoch") == epoch and v.get("fp") == fp}
    if len(valid) != len(entries):   # 惰性剔除过期项
        from app.core.cache import cache_set
        cache_set(_entry_key(kb_ids), valid)
    if not valid:
        return None
    hit = valid.get(nq)
    if not hit and qvec is not None:
        from app.providers.embedding import cosine
        cfg = get_settings()
        best_key, best_score = None, 0.0
        for k, v in valid.items():
            vec = v.get("vec")
            if not vec:
                continue
            s = cosine(qvec, vec)
            if s > best_score:
                best_key, best_score = k, s
        if best_key is not None and best_score >= cfg.ans_cache_sim_threshold:
            margin = best_score - max((cosine(qvec, v["vec"]) for k, v in valid.items()
                                       if k != best_key and v.get("vec")), default=0.0)
            if margin >= cfg.ans_cache_margin:
                hit = valid[best_key]
    if not hit:
        return None
    cache_set("wl2:ans_hits", int(cache_get("wl2:ans_hits") or 0) + 1)
    return hit.get("payload") or {}


def metrics() -> dict:
    """命中率指标（用量看板可见）：查询次数 / 命中次数。"""
    from app.core.cache import cache_get
    lookups = int(cache_get("wl2:ans_lookups") or 0)
    hits = int(cache_get("wl2:ans_hits") or 0)
    return {"lookups": lookups, "hits": hits,
            "hit_rate": round(hits / lookups, 4) if lookups else 0.0}


def put(kb_ids: list[int], question: str, qvec: list[float] | None, payload: dict) -> None:
    """写缓存（仅本地可信结果：调用方已过滤联网/降级/拒答/多轮场景）。"""
    if not cache_enabled():
        return
    nq = _norm(question)
    if not nq:
        return
    entries = _load(kb_ids)
    entries[nq] = {"epoch": kb_epoch(kb_ids), "fp": version_fingerprint(kb_ids),
                   "vec": qvec, "payload": payload}
    from app.core.cache import cache_set
    cache_set(_entry_key(kb_ids), entries, ttl=_DEFAULT_TTL)


def invalidate_kb(kb_ids: list[int] | None = None) -> None:
    """主动失效：指定库集合或全部（文档删除等即时生效场景）。"""
    from app.core.cache import cache_delete, cache_get, cache_set
    if kb_ids:
        cache_delete(_entry_key(kb_ids))
        return
    # 未指定：清所有答案缓存键（进程内字典遍历 + Redis 忽略——靠纪元兜底）
    from app.core.cache import _memory
    for k in [k for k in list(_memory.keys()) if k.startswith(_PREFIX)]:
        cache_delete(k)
