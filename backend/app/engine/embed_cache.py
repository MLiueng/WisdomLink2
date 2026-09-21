"""嵌入缓存（B-04）：clean_hash→向量 复用，重建/重处理只嵌入变更分片。

此前任何重建（换清洗规则/换分片策略/批量重处理）都全量重嵌；`clean_hash`
已存在但嵌入不复用。现在入库与 QA 重建统一走 `embed_with_cache`：
  - 命中（clean_hash+embed_model 相同）直接复用缓存向量，不调嵌入服务、不计费；
  - 未命中分片才批量嵌入并回写缓存；
  - 计量口径只统计未命中分片——重建未变更文档的嵌入调用数/记账为 0（验收①②）。
缓存按模型隔离：换嵌入模型后 key 不同，自然全量重嵌（recon 可检出混维）。
"""
import json
from sqlalchemy import select
from app.db import SessionLocal
from app.models import EmbedCache
from app.core.metering import record_usage


def _fetch(hashes: list[str], model_id: str) -> dict[str, list[float]]:
    if not hashes:
        return {}
    db = SessionLocal()
    try:
        rows = db.scalars(select(EmbedCache).where(
            EmbedCache.clean_hash.in_(hashes), EmbedCache.embed_model == model_id)).all()
        out: dict[str, list[float]] = {}
        for r in rows:
            try:
                out[r.clean_hash] = json.loads(r.vector)
            except (json.JSONDecodeError, TypeError):
                continue
        return out
    finally:
        db.close()


def _put(items: list[tuple[str, str, list[float]]]) -> None:
    if not items:
        return
    db = SessionLocal()
    try:
        for h, m, v in items:
            db.merge(EmbedCache(clean_hash=h, embed_model=m, vector=json.dumps(v)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def embed_with_cache(emb, texts: list[str], hashes: list[str] | None = None,
                           purpose: str = "embed", kb_id: int | None = None) -> list[list[float]]:
    """缓存感知嵌入：命中复用、未命中才嵌入并回写缓存；返回与 texts 同序的向量。

    hashes 缺省时按文本内容自算 sha256（QA 问句同文复用同一向量）。
    全部命中时不调嵌入服务、不产生任何计量记录（验收：重建未变更嵌入调用=0）。
    """
    import asyncio
    from app.core import rules
    if not texts:
        return []
    hs = hashes if hashes is not None else [rules.sha256_text(t) for t in texts]
    model_id = getattr(emb, "model_id", "") or getattr(emb, "name", "unknown")
    # P-02：缓存读/回写为同步 DB 段，卸载线程池（热路径经图谱向量链接可达）
    cached = await asyncio.to_thread(_fetch, hs, model_id)
    miss_idx = [i for i, h in enumerate(hs) if h not in cached]
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        miss_vecs = await emb.embed(miss_texts)
        for i, v in zip(miss_idx, miss_vecs):
            cached[hs[i]] = v
        await asyncio.to_thread(_put, [(hs[i], model_id, v) for i, v in zip(miss_idx, miss_vecs)])
        record_usage(model_id, getattr(emb, "vendor", getattr(emb, "name", "")), purpose, kb_id,
                     prompt_tokens=sum(len(t) for t in miss_texts) // 2,
                     is_estimated=(getattr(emb, "name", "") == "mock"))
    return [cached[h] for h in hs]
