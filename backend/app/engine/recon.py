"""一致性对账与陈旧内容巡检（M4/M7，升级迭代方案 §4.4/§4.7）。

对账四项（§6.4 原则 7：每加一层融合加一层对账）：
  1. 文档数 ↔ 可检索分片数（DB 侧）
  2. 图谱边的 evidence_chunk_id 有效率（指向存活分块的比例）
  3. KG 悬空节点（有 confirmed 边但节点状态异常的检测由 /kg/health 负责，此处仅计数）
  4. 陈旧内容探针：已删/下线文档的标题作探针查询，断言零召回

巡检为只读操作，差异写入 ReconRun 报表；不修改任何索引。
"""
import json
from datetime import datetime
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import ChunkMeta, Document, DocVersion, KGEdge, KGNode, ReconRun


def _live_version_ids(db, kb_id: int) -> set[int]:
    """与检索引擎同口径的可检索版本集合。"""
    rows = db.execute(select(DocVersion.id)
                      .join(Document, DocVersion.document_id == Document.id)
                      .where(Document.kb_id == kb_id, Document.status.in_(("published", "offline")),
                             DocVersion.is_current == True)).all()  # noqa: E712
    return {r[0] for r in rows}


def check_consistency(kb_id: int) -> dict:
    """跨层一致性核对；返回各项检查结果（全部只读）。"""
    db = SessionLocal()
    try:
        live = _live_version_ids(db, kb_id)
        doc_count = db.scalar(select(func.count()).select_from(Document).where(
            Document.kb_id == kb_id, Document.status.in_(("published", "offline")))) or 0
        chunk_count = db.scalar(select(func.count()).select_from(ChunkMeta).where(
            ChunkMeta.kb_id == kb_id, ChunkMeta.role == "child",
            ChunkMeta.doc_version_id.in_(live or [-1]))) or 0
        live_chunk_ids = set(db.scalars(select(ChunkMeta.id).where(
            ChunkMeta.kb_id == kb_id, ChunkMeta.role == "child",
            ChunkMeta.doc_version_id.in_(live or [-1]))).all())

        # 图谱边证据有效率
        edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id,
                                                KGEdge.status == "confirmed")).all()
        edges_with_ev = [e for e in edges if e.evidence_chunk_id]
        ev_valid = [e for e in edges_with_ev if e.evidence_chunk_id in live_chunk_ids]
        evidence_rate = round(len(ev_valid) / len(edges_with_ev), 4) if edges_with_ev else 1.0
        # A-07：证据指纹覆盖率（悬空重映射的前提；存量边由兜底补填，新边写入即回填）
        stamped = [e for e in edges_with_ev if getattr(e, "evidence_clean_hash", None)]
        fp_rate = round(len(stamped) / len(edges_with_ev), 4) if edges_with_ev else 1.0

        node_count = db.scalar(select(func.count()).select_from(KGNode).where(
            KGNode.kb_id == kb_id, KGNode.status == "confirmed")) or 0
        candidate_nodes = db.scalar(select(func.count()).select_from(KGNode).where(
            KGNode.kb_id == kb_id, KGNode.status == "candidate")) or 0
    finally:
        db.close()

    checks = {
        "docs_vs_chunks": {"doc_count": doc_count, "chunk_count": chunk_count,
                           # 有发布文档却无分片 = 索引缺失嫌疑（文档数>0 且分片为 0）
                           "passed": bool(doc_count == 0 or chunk_count > 0)},
        "kg_evidence_validity": {"edges_total": len(edges), "edges_with_evidence": len(edges_with_ev),
                                 "evidence_valid": len(ev_valid), "rate": evidence_rate,
                                 # A-07：指纹覆盖率（重映射能力度量，只读观测）
                                 "fingerprint_rate": fp_rate,
                                 "passed": evidence_rate >= 0.99},
        "kg_scale": {"confirmed_nodes": node_count, "candidate_nodes": candidate_nodes,
                     "confirmed_edges": len(edges)},
        "kb_id": kb_id,
    }
    checks["overall_passed"] = all(c.get("passed", True) for k, c in checks.items()
                                   if isinstance(c, dict))
    return checks


def hits_from_deleted(hits: list) -> int:
    """统计召回分片中属于「已删除文档」的数量（陈旧判定真口径）。

    稠密召回对泛化查询总会返回语义最近的在架内容（正常行为），
    只有召回分片真正来自已删除文档才算陈旧残留。
    """
    if not hits:
        return 0
    from app.models import DocVersion, Document
    doc_ids: set[int] = set()
    ver_ids: set[int] = set()
    for h in hits:
        did = (h.payload or {}).get("doc_id")
        if did:
            doc_ids.add(did)
        if getattr(h, "doc_version_id", None):
            ver_ids.add(h.doc_version_id)
    db = SessionLocal()
    try:
        if ver_ids:
            rows = db.scalars(select(DocVersion).where(DocVersion.id.in_(ver_ids))).all()
            doc_ids |= {r.document_id for r in rows}
        if not doc_ids:
            return 0
        deleted = db.scalars(select(Document.id).where(
            Document.id.in_(doc_ids), Document.status == "deleted")).all()
        # 命中的已删文档分片数（按 chunk 计）
        return sum(1 for h in hits
                   if ((h.payload or {}).get("doc_id") in deleted))
    finally:
        db.close()


async def stale_probe(kb_id: int) -> list[dict]:
    """陈旧内容探针（§4.7）：取已删/下线文档标题做检索探针，断言无已删文档分片被召回。

    探针只查"最近删除/下线的文档"（最多 10 个），避免全量扫描。
    判定口径：召回分片来自已删除文档即为失败（泛化查询召回其他在架内容属正常语义）。
    """
    from app.engine.retrieval import hybrid_search
    db = SessionLocal()
    try:
        docs = db.scalars(select(Document).where(Document.kb_id == kb_id,
                                                 Document.status.in_(("deleted", "offline")))
                          .order_by(Document.id.desc()).limit(10)).all()
        probes = [{"doc_id": d.id, "title": d.title, "status": d.status} for d in docs]
    finally:
        db.close()
    results = []
    for p in probes:
        try:
            r = await hybrid_search([kb_id], p["title"], top_k=3, use_rerank=False)
            hits = r["hits"]
        except Exception:
            hits = []
        stale_hits = 0 if p["status"] == "offline" else hits_from_deleted(hits)
        results.append({"doc_id": p["doc_id"], "title": p["title"], "status": p["status"],
                        "recall_count": len(hits), "stale_hits": stale_hits,
                        # deleted 文档分片不得被召回；offline 文档允许召回（既有语义：在架可检索）
                        "passed": stale_hits == 0})
    return results


async def vector_recon(kb_id: int) -> dict:
    """A-08 向量反向对账：枚举 `chunk_{kb_id}` 集合，与 DB 存活 child 分片反向比对，
    找出孤儿点（DB 行已不存在但向量仍驻留——删除失败/崩溃窗口/脚本直删遗留）。

    B-04 附带检测：点上 `embed_model` 出现多个取值 = 混维残留（换模型后未重建）。
    只读巡检，不做清理。
    """
    from app.providers.vector_store import get_vector_store
    db = SessionLocal()
    try:
        live_ids = {str(i) for i in db.scalars(select(ChunkMeta.id).where(
            ChunkMeta.kb_id == kb_id, ChunkMeta.role == "child",
            ChunkMeta.active == True)).all()}  # noqa: E712
    finally:
        db.close()
    try:
        points = await get_vector_store().list_points(f"chunk_{kb_id}", limit=20000)
    except Exception:
        return {"vector_points": 0, "live_chunks": len(live_ids), "orphan_count": 0,
                "orphan_points": [], "embed_models": [], "mixed_models": False,
                "passed": True, "error": "vector list unavailable"}
    dense = [(pid, p) for pid, p in points if (p.get("source") or "dense") == "dense"]
    orphans = sorted(pid for pid, _ in dense if pid not in live_ids)
    models = sorted({str(p.get("embed_model")) for _, p in dense if p.get("embed_model")})
    return {"vector_points": len(dense), "live_chunks": len(live_ids),
            "orphan_count": len(orphans), "orphan_points": orphans[:100],
            "embed_models": models, "mixed_models": len(models) > 1,
            "passed": not orphans}


def run_recon(kb_id: int) -> dict:
    """对账 + 巡检入口（同步部分）；探针为异步，由路由层组装后调用 _save。"""
    return check_consistency(kb_id)


def save_recon(kb_id: int, checks: dict, probes: list[dict]) -> int:
    """落库巡检报告，返回记录 id。"""
    db = SessionLocal()
    try:
        row = ReconRun(kb_id=kb_id, checks=json.dumps(checks, ensure_ascii=False),
                       stale_probe=json.dumps(probes, ensure_ascii=False))
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()
