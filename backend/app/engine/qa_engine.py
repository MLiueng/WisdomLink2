"""QA 快速命中引擎（FR-131 / BR-011 / NFR-264 / ADR-012）。"""
import asyncio
import json
from datetime import date, timedelta
from sqlalchemy import select
from app.config import get_settings
from app.core.cache import cache_delete, cache_get, cache_set
from app.core.metering import record_usage
from app.core.rules import normalize_question, qa_pass_thresholds, sha256_text
from app.db import SessionLocal
from app.models import QAPair
from app.providers.embedding import get_embedding
from app.providers.vector_store import get_vector_store

DICT_KEY = "qa_dict:{kb}"


def _variants(qa: QAPair) -> list[str]:
    try:
        return json.loads(qa.variants or "[]")
    except json.JSONDecodeError:
        return []


def qa_questions(qa: QAPair) -> list[str]:
    return [qa.std_question] + _variants(qa)


def refresh_dict(kb_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.scalars(select(QAPair).where(QAPair.kb_id == kb_id, QAPair.enabled == True)).all()  # noqa: E712
    finally:
        db.close()
    d: dict[str, int] = {}
    for qa in rows:
        for q in qa_questions(qa):
            nq = normalize_question(q)
            if nq:
                d.setdefault(nq, qa.id)
    cache_set(DICT_KEY.format(kb=kb_id), d)
    return d


def invalidate(kb_id: int) -> None:
    cache_delete(DICT_KEY.format(kb=kb_id))


async def reindex_qa(kb_id: int, qa_ids: list[int] | None = None) -> int:
    """QA 向量集合重建/增量更新（B-06）。

    qa_ids=None：全量重建（先整集合清空，审计 H3——避免删除/停用后的旧点残留，
    以及历史驱动遗留的重复点污染双阈值判定）。
    qa_ids=[...]：增量——仅删除对应 QA 的点再重嵌启用项，不整集合重建：
    无快路径空窗、不重嵌无关条目（此前任何编辑都全集合重嵌）。
    """
    db = SessionLocal()
    try:
        q = select(QAPair).where(QAPair.kb_id == kb_id)
        if qa_ids:
            q = q.where(QAPair.id.in_(qa_ids))
        rows = db.scalars(q).all()
    finally:
        db.close()
    emb = get_embedding()
    vs = get_vector_store()
    ids, texts, payloads = [], [], []
    for qa in rows:
        if not qa.enabled:
            continue   # B-06：停用项不嵌入（其旧点由下方 delete 清除）
        for qi, qtext in enumerate(qa_questions(qa)):
            ids.append(f"{qa.id}:{qi}")
            texts.append(qtext)
            payloads.append({"text": qtext, "qa_id": qa.id, "weight": qa.weight,
                             "enabled": qa.enabled, "folder_id": qa.folder_id, "source": "qa"})
    try:
        if qa_ids:
            await vs.delete_by(f"qa_{kb_id}", {"in": {"qa_id": list(qa_ids)}})
        else:
            await vs.drop_collection(f"qa_{kb_id}")
    except Exception:
        # 集合不存在属预期（首次建库）；其余失败（网络/驱动）一旦吞掉，旧向量点会
        # 残留污染双阈值判定（审计 H3）——必须留痕供排查，upsert 继续由驱动兜底
        from app.core.logging_config import setup_logging
        setup_logging().warning("QA 旧向量清理失败（kb=%s qa_ids=%s）", kb_id, qa_ids, exc_info=True)
    if ids:
        # B-04：嵌入缓存——问句文本稳定，重嵌时复用向量、只计量未命中部分
        from app.engine.embed_cache import embed_with_cache
        vecs = await embed_with_cache(emb, texts, purpose="qa_embed", kb_id=kb_id)
        if vs.name == "qdrant":
            await vs.ensure_collection(f"qa_{kb_id}", len(vecs[0]))
        await vs.upsert(f"qa_{kb_id}", ids, vecs, payloads)
    refresh_dict(kb_id)
    return len(ids)


async def generate_variants(question: str, answer: str, existing: list[str]) -> list[str]:
    """B-05 LLM 自动生成改写变体（灰度 `runtime_qa_variants=on` 才生效）。

    产出 3-5 条与标准问题语义一致、表述不同的问法；与标准问题/已有变体归一化去重。
    生成失败或格式异常时返回空列表（回退纯手工录入），绝不阻塞 QA 创建。
    """
    from app.core.runtime_config import get_runtime
    if (get_runtime("runtime_qa_variants") or "off") != "on":
        return []
    from app.providers.llm import get_llm
    prompt = (
        "为知识库标准问题生成 3-5 条改写变体（同义不同表述，口语/书面均可），"
        "用于问答快速命中。严格输出 JSON 数组（不要任何解释文字），例如 [\"变体1\",\"变体2\",\"变体3\"]。\n"
        f"标准问题：{question}\n参考答案：{answer[:200]}"
    )
    try:
        llm = get_llm()
        text, usage = await llm.chat([{"role": "user", "content": prompt}], temperature=0.7)
        record_usage(llm.model_id, getattr(llm, "vendor", llm.name), "qa_gen", None,
                     usage.prompt_tokens, usage.completion_tokens, is_estimated=usage.is_estimated)
    except Exception:
        return []
    import re
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    seen = {normalize_question(question)} | {normalize_question(v) for v in existing}
    out: list[str] = []
    for v in arr:
        v = str(v).strip()
        if not v or normalize_question(v) in seen:
            continue
        seen.add(normalize_question(v))
        out.append(v)
        if len(out) >= 5:
            break
    return out


def exact_hit(kb_ids: list[int], question: str) -> dict | None:
    """归一化精确命中（O(1) 字典，无需向量）：按会话库顺序，首个命中即返回。"""
    nq = normalize_question(question)
    for kb in kb_ids:
        d = cache_get(DICT_KEY.format(kb=kb))
        if d is None:
            d = refresh_dict(kb)
        if nq and nq in d:
            return _hit_payload(d[nq], "exact", 1.0)
    return None


async def similar_hit(kb_ids: list[int], question: str, qvec: list[float],
                      folder_ids: list[int] | None = None) -> dict | None:
    """QA 向量相似命中（双阈值 BR-011）；qvec 由调用方透传（P-03 避免重复嵌入）。"""
    vs = get_vector_store()
    candidates: list[tuple[float, int, int, int]] = []  # score, weight, kb_order, qa_id
    for order, kb in enumerate(kb_ids):
        flt = {"in": {"enabled": [True]}}
        if folder_ids:
            flt["in"]["folder_id"] = folder_ids
        # FAISS 不支持过滤下推，但基类 search 会做内存后过滤：
        # 过滤条件必须保留，否则停用 QA 仍可相似命中（审计 M2）
        for h in await vs.search(f"qa_{kb}", qvec, 5, flt):
            p = h.payload or {}
            candidates.append((h.score, int(p.get("weight", 100)), -order, int(p.get("qa_id", 0))))
    candidates.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    # 按 qa_id 去重（同一 QA 多问句向量/历史重复点会产生多个候选，
    # 若不去重，并列高分使 margin 判定恒失败，相似命中被永久杀死）
    seen: set[int] = set()
    deduped = []
    for c in candidates:
        if c[3] in seen:
            continue
        seen.add(c[3])
        deduped.append(c)
    candidates = deduped
    cfg = get_settings()
    scores = [c[0] for c in candidates]
    if not qa_pass_thresholds(scores, cfg.qa_sim_threshold, cfg.qa_margin):
        return None
    # P-02：命中载荷构建含 hit_count 自增写（同步事务），卸载线程池
    return await asyncio.to_thread(_hit_payload, candidates[0][3], "similar", scores[0])


async def try_hit(kb_ids: list[int], question: str, folder_ids: list[int] | None = None,
                  qvec: list[float] | None = None) -> dict | None:
    """两级判定：归一化精确（O(1) 字典）→ QA 向量相似（双阈值 BR-011）。命中即秒回。

    P-03：qvec 可由入口透传（同一问题仅嵌入一次）；缺省时内部现算并按旧口径计量。
    P-02：精确命中含缓存刷新/命中计数的同步 DB 段，卸载线程池不阻塞事件循环。
    """
    hit = await asyncio.to_thread(exact_hit, kb_ids, question)
    if hit:
        return hit
    if qvec is None:
        emb = get_embedding()
        qvec = (await emb.embed([question]))[0]
        record_usage(emb.model_id, getattr(emb, "vendor", emb.name), "qa_embed", kb_ids[0] if kb_ids else None,
                     prompt_tokens=len(question) // 2, is_estimated=(emb.name == "mock"))
    return await similar_hit(kb_ids, question, qvec, folder_ids)


async def gate(kb_ids: list[int], question: str,
               folder_ids: list[int] | None = None) -> tuple[dict | None, list[float] | None]:
    """P-03 QA 门 + 查询向量一体化入口：返回 (命中, 查询向量)。

    精确命中直接返回（不付嵌入成本，保住秒回路径）；否则仅嵌入一次，
    完成相似判定后把向量交还调用方，供首轮混合检索复用（此前两处各嵌一次）。
    嵌入失败时按旧口径重试一次（再失败则异常上抛，与旧行为一致）。
    """
    hit = await asyncio.to_thread(exact_hit, kb_ids, question)
    if hit:
        return hit, None
    qvec = None
    try:
        emb = get_embedding()
        qvec = (await emb.embed([question]))[0]
        record_usage(emb.model_id, getattr(emb, "vendor", emb.name), "qa_embed", kb_ids[0] if kb_ids else None,
                     prompt_tokens=len(question) // 2, is_estimated=(emb.name == "mock"))
    except Exception:
        qvec = None
    if qvec is None:
        return await try_hit(kb_ids, question, folder_ids), None
    return await similar_hit(kb_ids, question, qvec, folder_ids), qvec


def _hit_payload(qa_id: int, mode: str, score: float) -> dict | None:
    from sqlalchemy import update
    from datetime import datetime
    db = SessionLocal()
    try:
        qa = db.get(QAPair, qa_id)
        if not qa or not qa.enabled:
            return None
        # SQL 级自增，避免并发命中时读-改-写丢失更新（审计 M1）
        db.execute(update(QAPair).where(QAPair.id == qa_id).values(
            hit_count=QAPair.hit_count + 1, last_hit_at=datetime.now()))
        db.commit()
        doc_ids = _safe_list(qa.ref_doc_ids)
        return {"qa_id": qa.id, "mode": mode, "score": round(score, 4),
                "kb_id": qa.kb_id, "question": qa.std_question, "answer": qa.std_answer,
                "ref_doc_ids": doc_ids, "updated_at": str(qa.created_at or ""),
                "review_due_at": str(qa.review_due_at or "")}
    finally:
        db.close()


def _safe_list(raw: str) -> list:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def estimate_saved() -> int:
    """QA 命中节省 token 估算口径（BR-012：单次 RAG 生成约 1200 token）。"""
    return 1200


def ensure_review_dates() -> None:
    """创建时回填复审到期日（R12 / B-31）。"""
    db = SessionLocal()
    try:
        rows = db.scalars(select(QAPair).where(QAPair.review_due_at == None)).all()  # noqa: E711
        for qa in rows:
            qa.review_due_at = date.today() + timedelta(days=get_settings().qa_review_days)
        db.commit()
    finally:
        db.close()


def text_fingerprint(text: str) -> str:
    return sha256_text(text)
