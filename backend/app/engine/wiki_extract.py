"""变更驱动抽取（M3 / L3，升级迭代方案 §4.3）：Wiki 发布 → 抽取候选队列 → 人工确认入图。

设计要点（分析文档 §5.4）：
  - 触发点：文档发布（入库流水线完成 / 下线转发布）；仅 source_type=wiki 页面触发
  - 去重：候选以「文档+版本」指纹唯一，重复触发不入队（避免并发重复，§2.3.2 教训）
  - 异步：抽取在后台任务执行，不阻塞发布流程；LLM 失败仅告警不影响发布
  - 新鲜度：页面改版时，旧版本证据支撑的已确认关系降级为候选（待复核），
    避免「页面已改、关系仍旧」（§5.4 新鲜度错位）
  - 确认通道：候选确认后才写入图谱候选态（沿用既有 /kg 确认流程完成最终确认）
"""
import asyncio
import json
from datetime import datetime
from sqlalchemy import select
from app.core.logging_config import setup_logging
from app.core.rules import sha256_text
from app.db import SessionLocal
from app.models import ChunkMeta, DocVersion, Document, KGEdge, KgCandidate

log = setup_logging()

MAX_CHUNKS_PER_PAGE = 15   # 单页面抽取的片段上限（控制 LLM 成本）


def _runtime_flag(key: str, default: str) -> str:
    from app.core.runtime_config import get_runtime
    val = get_runtime(key)
    return val or default


def wiki_extract_enabled() -> bool:
    """灰度开关：sys_config runtime_wiki_extract（on|off），缺省 on。"""
    return _runtime_flag("runtime_wiki_extract", "on") == "on"


def _candidate_fingerprint(doc_id: int, version_id: int) -> str:
    return sha256_text(f"doc:{doc_id}:v{version_id}")[:64]


async def extract_doc_to_candidate(doc_id: int) -> dict:
    """对单个文档执行增量抽取，产出候选（不直接入图）。返回 {candidate_id|skipped, ...}。"""
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or doc.status != "published":
            return {"skipped": True, "reason": "文档不存在或未发布"}
        ver = db.scalar(select(DocVersion).where(DocVersion.document_id == doc_id,
                                                 DocVersion.is_current == True))  # noqa: E712
        if not ver:
            return {"skipped": True, "reason": "无当前版本"}
        fp = _candidate_fingerprint(doc_id, ver.id)
        dup = db.scalar(select(KgCandidate).where(KgCandidate.kb_id == doc.kb_id,
                                                  KgCandidate.fingerprint == fp))
        if dup:
            return {"skipped": True, "reason": "同版本候选已存在", "candidate_id": dup.id}
        chunks = db.scalars(select(ChunkMeta).where(ChunkMeta.doc_version_id == ver.id,
                                                    ChunkMeta.role == "child")
                            .order_by(ChunkMeta.seq).limit(MAX_CHUNKS_PER_PAGE)).all()
        kb_id, version_id = doc.kb_id, ver.id
    finally:
        db.close()
    if not chunks:
        return {"skipped": True, "reason": "无可用片段"}

    # LLM 抽取（复用手动抽取同款提示词）
    from app.engine.kge import EXTRACT_PROMPT, parse_extract_json
    from app.providers.llm import get_llm
    from app.core.metering import record_usage
    blocks = "\n".join(f"[{c.id}] {(c.text or '')[:600]}" for c in chunks)
    llm = get_llm()
    try:
        raw, usage = await llm.chat([
            {"role": "system", "content": "只输出 JSON，不要任何解释文字。"},
            {"role": "user", "content": EXTRACT_PROMPT.replace("{blocks}", blocks)},
        ], temperature=0, max_tokens=2048)
        data = parse_extract_json(raw)
    except Exception as e:
        log.warning("变更驱动抽取失败（候选不入队，等待下次发布或手动触发） | doc=%s | %s", doc_id, e)
        return {"skipped": True, "reason": f"抽取失败: {e}"}
    if usage:
        record_usage(llm.model_id, getattr(llm, "vendor", llm.name), "extract", kb_id,
                     usage.prompt_tokens, usage.completion_tokens, usage.is_estimated)

    valid_ids = [c.id for c in chunks]
    payload = {"entities": (data.get("entities") or [])[:60],
               "relations": (data.get("relations") or [])[:100],
               "chunk_ids": valid_ids}
    db = SessionLocal()
    try:
        cand = KgCandidate(kb_id=kb_id, doc_id=doc_id, doc_version_id=version_id,
                           fingerprint=fp, payload=json.dumps(payload, ensure_ascii=False),
                           status="pending")
        db.add(cand)
        db.commit()
        cid = cand.id
    except Exception:
        # 唯一约束冲突（并发重复触发）：视为已入队
        db.rollback()
        return {"skipped": True, "reason": "并发重复触发"}
    finally:
        db.close()
    log.info("变更驱动抽取入队 | doc=%s | kb=%s | candidate=%s | entities=%d | relations=%d",
             doc_id, kb_id, cid, len(payload["entities"]), len(payload["relations"]))
    return {"candidate_id": cid, "entities": len(payload["entities"]),
            "relations": len(payload["relations"])}


def on_doc_published(doc_id: int) -> None:
    """发布钩子（同步入口，供流水线/路由调用）：wiki 页面才触发；后台任务执行抽取。"""
    if not wiki_extract_enabled():
        return
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or doc.source_type != "wiki":
            return
        ver = db.scalar(select(DocVersion).where(DocVersion.document_id == doc_id,
                                                 DocVersion.is_current == True))  # noqa: E712
        ver_id = ver.id if ver else 0
    finally:
        db.close()
    if not ver_id:
        return
    # 新鲜度对账：旧版本证据支撑的已确认关系降级为候选（待复核）
    _demote_stale_edges(doc_id, ver_id)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_extract_safe(doc_id))
    except RuntimeError:
        # 无事件循环（同步端点/守护线程）：后台线程独立循环执行，不阻塞调用方
        import threading
        threading.Thread(target=asyncio.run, args=(_extract_safe(doc_id),),
                         name=f"wl2-kge-{doc_id}", daemon=True).start()


async def _extract_safe(doc_id: int) -> None:
    try:
        await extract_doc_to_candidate(doc_id)
    except Exception:
        log.exception("变更驱动抽取任务异常 | doc=%s", doc_id)


def _demote_stale_edges(doc_id: int, current_version_id: int) -> int:
    """页面改版：旧版本分片支撑的 confirmed 边降级为 candidate（待复核）。"""
    db = SessionLocal()
    try:
        old_chunk_ids = set(db.scalars(select(ChunkMeta.id).where(
            ChunkMeta.doc_version_id != current_version_id,
            ChunkMeta.doc_version_id.in_(
                select(DocVersion.id).where(DocVersion.document_id == doc_id)))).all())
        if not old_chunk_ids:
            return 0
        edges = db.scalars(select(KGEdge).where(KGEdge.status == "confirmed",
                                                KGEdge.evidence_chunk_id.in_(old_chunk_ids))).all()
        n = 0
        for e in edges:
            e.status = "candidate"
            n += 1
        if n:
            db.commit()
            from app.core.metering import audit
            audit("kg.edges.demote", "document", doc_id,
                  detail={"count": n, "reason": "页面改版，关系待复核（M3 新鲜度对账）"})
            log.info("页面改版关系降级待复核 | doc=%s | edges=%d", doc_id, n)
        return n
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


def confirm_candidate(candidate_id: int, actor: str = "admin") -> dict:
    """候选确认：payload 写入图谱候选态（沿用 /kg 人工确认流程做最终确认）。"""
    db = SessionLocal()
    try:
        cand = db.get(KgCandidate, candidate_id)
        if not cand:
            return {"ok": False, "error": "候选不存在"}
        if cand.status != "pending":
            return {"ok": False, "error": f"候选已处理（{cand.status}）"}
        payload = json.loads(cand.payload or "{}")
        # 复用手动抽取的入图逻辑（候选态），保证与既有确认流程一致
        from app.api.routers.kg import _apply_extract_to_graph
        ne, nr = _apply_extract_to_graph(db, cand.kb_id, payload, set(payload.get("chunk_ids", [])))
        cand.status = "confirmed"
        cand.reviewed_at = datetime.now()
        db.commit()
        # B-07：引擎层写图点主动失效图缓存（指纹校验可兜底，此处加速生效）
        try:
            from app.engine.kg_graph import invalidate_graph_cache, invalidate_node_vecs
            invalidate_graph_cache(cand.kb_id)
            invalidate_node_vecs(cand.kb_id)
        except Exception:
            pass
        from app.core.metering import audit
        audit("kg.candidate.confirm", "kg_candidate", candidate_id,
              detail={"entities": ne, "relations": nr}, actor=actor)
        return {"ok": True, "entities": ne, "relations": nr}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)[:200]}
    finally:
        db.close()


def reject_candidate(candidate_id: int, actor: str = "admin") -> dict:
    db = SessionLocal()
    try:
        cand = db.get(KgCandidate, candidate_id)
        if not cand:
            return {"ok": False, "error": "候选不存在"}
        if cand.status != "pending":
            return {"ok": False, "error": f"候选已处理（{cand.status}）"}
        cand.status = "rejected"
        cand.reviewed_at = datetime.now()
        db.commit()
        from app.core.metering import audit
        audit("kg.candidate.reject", "kg_candidate", candidate_id, actor=actor)
        return {"ok": True}
    finally:
        db.close()
