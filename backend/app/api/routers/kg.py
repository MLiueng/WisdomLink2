"""知识图谱（FR-117/121）：概念/实体/关系 CRUD、抽取校对、导入导出、实体链接与扩展。"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from app.core.metering import audit
from app.core.security import require_admin
from app.db import SessionLocal
from app.providers.kg_store import KGStore
from app.models import ChunkMeta, KGConcept, KGEdge, KGNode

router = APIRouter(prefix="/api/kg", tags=["kg"])


class ConceptIn(BaseModel):
    name: str
    parent_id: int | None = None
    synonyms: list[str] = []


class NodeIn(BaseModel):
    name: str
    concept_id: int | None = None
    aliases: list[str] = []
    status: str = "candidate"


class EdgeIn(BaseModel):
    src_id: int
    dst_id: int
    relation: str
    evidence_chunk_id: int | None = None
    status: str = "candidate"


def _pl(raw: str) -> list:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def _invalidate_kg(kb_id: int) -> None:
    """B-07：图谱写操作后失效库级图缓存与实体向量缓存（幂等，失败不阻断主流程）。"""
    try:
        from app.engine.kg_graph import invalidate_graph_cache, invalidate_node_vecs
        invalidate_graph_cache(kb_id)
        invalidate_node_vecs(kb_id)
    except Exception:
        pass


@router.get("/{kb_id}/concepts", summary="概念列表（本体 is-a 层级+同义词）",)
def concepts(kb_id: int):
    db = SessionLocal()
    try:
        rows = db.scalars(select(KGConcept).where(KGConcept.kb_id == kb_id)).all()
        return [{"id": c.id, "name": c.name, "parent_id": c.parent_id, "synonyms": _pl(c.synonyms)}
                for c in rows]
    finally:
        db.close()


@router.post("/{kb_id}/concepts", dependencies=[Depends(require_admin)], summary="新增概念",)
def add_concept(kb_id: int, body: ConceptIn):
    db = SessionLocal()
    try:
        c = KGConcept(kb_id=kb_id, name=body.name, parent_id=body.parent_id,
                      synonyms=json.dumps(body.synonyms, ensure_ascii=False))
        db.add(c)
        db.commit()
        return {"id": c.id}
    finally:
        db.close()


@router.get("/{kb_id}/nodes", summary="实体列表（分页；候选/已确认状态筛选）",)
def nodes(kb_id: int, status: str | None = None, page: int = 1, size: int = 20):
    db = SessionLocal()
    try:
        conds = [KGNode.kb_id == kb_id] + ([KGNode.status == status] if status else [])
        total = db.scalar(select(func.count()).select_from(KGNode).where(*conds)) or 0
        rows = db.scalars(select(KGNode).where(*conds).order_by(KGNode.id)
                          .offset((page - 1) * size).limit(size)).all()
        return {"total": total, "items": [{"id": n.id, "name": n.name, "concept_id": n.concept_id,
                 "aliases": _pl(n.aliases), "status": n.status} for n in rows]}
    finally:
        db.close()


@router.get("/{kb_id}/graph", summary="图数据（ECharts 力导向图用；confirmed/all）")
def graph(kb_id: int, status: str = "confirmed"):
    db = SessionLocal()
    try:
        node_cond = [KGNode.kb_id == kb_id] + ([KGNode.status == status] if status in ("confirmed", "candidate") else [])
        nodes = db.scalars(select(KGNode).where(*node_cond).limit(300)).all()
        edge_cond = [KGEdge.kb_id == kb_id] + ([KGEdge.status == status] if status in ("confirmed", "candidate") else [])
        edges = db.scalars(select(KGEdge).where(*edge_cond).limit(500)).all()
        concepts = db.scalars(select(KGConcept).where(KGConcept.kb_id == kb_id)).all()
    finally:
        db.close()
    idset = {n.id for n in nodes}
    concepts = [c for c in concepts]
    cat_names = [c.name for c in concepts[:8]] + ["其他"]
    def cat_of(n):
        cid = n.concept_id
        name = next((c.name for c in concepts if c.id == cid), None)
        return cat_names.index(name) if name in cat_names else len(cat_names) - 1
    gnodes = [{"id": str(n.id), "name": n.name, "category": cat_of(n),
               "symbolSize": 34 if n.status == "confirmed" else 22,
               "itemStyle": {"borderType": "solid" if n.status == "confirmed" else "dashed"}} for n in nodes]
    glinks = [{"source": str(e.src_id), "target": str(e.dst_id), "relation": e.relation,
               "value": e.status} for e in edges if e.src_id in idset and e.dst_id in idset]
    return {"categories": cat_names, "nodes": gnodes, "links": glinks}


@router.post("/{kb_id}/nodes", dependencies=[Depends(require_admin)], summary="新增实体（候选态，确认后参与关联检索）",)
def add_node(kb_id: int, body: NodeIn):
    db = SessionLocal()
    try:
        n = KGNode(kb_id=kb_id, name=body.name, concept_id=body.concept_id,
                   aliases=json.dumps(body.aliases, ensure_ascii=False), status=body.status)
        db.add(n)
        db.commit()
        nid = n.id
    finally:
        db.close()
    _invalidate_kg(kb_id)
    return {"id": nid}


@router.patch("/nodes/{node_id}", dependencies=[Depends(require_admin)], summary="编辑实体（别名/概念挂接/状态）",)
def patch_node(node_id: int, body: NodeIn):
    db = SessionLocal()
    try:
        n = db.get(KGNode, node_id)
        if not n:
            raise HTTPException(404, "实体不存在")
        n.name, n.concept_id, n.status = body.name, body.concept_id, body.status
        n.aliases = json.dumps(body.aliases, ensure_ascii=False)
        kb_id = n.kb_id
        db.commit()
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：名称/别名/状态变更 → 图缓存与实体向量缓存失效
    return {"ok": True}


@router.get("/{kb_id}/edges", summary="关系列表（分页；含证据 chunk 与状态）",)
def edges(kb_id: int, status: str | None = None, page: int = 1, size: int = 20):
    db = SessionLocal()
    try:
        conds = [KGEdge.kb_id == kb_id] + ([KGEdge.status == status] if status else [])
        total = db.scalar(select(func.count()).select_from(KGEdge).where(*conds)) or 0
        rows = db.scalars(select(KGEdge).where(*conds).order_by(KGEdge.id)
                          .offset((page - 1) * size).limit(size)).all()
        nmap = {n.id: n.name for n in db.scalars(select(KGNode).where(KGNode.kb_id == kb_id)).all()}
        return {"total": total, "items": [{"id": e.id, "src": nmap.get(e.src_id, e.src_id), "dst": nmap.get(e.dst_id, e.dst_id),
                 "src_id": e.src_id, "dst_id": e.dst_id, "relation": e.relation,
                 "evidence_chunk_id": e.evidence_chunk_id, "status": e.status} for e in rows]}
    finally:
        db.close()


@router.post("/{kb_id}/edges", dependencies=[Depends(require_admin)], summary="新增关系边",)
def add_edge(kb_id: int, body: EdgeIn):
    db = SessionLocal()
    try:
        e = KGEdge(kb_id=kb_id, **body.model_dump())
        db.add(e)
        db.commit()
        # A-07：手工加边同样回填证据指纹（调用方提交后立即补填，失败不阻断）
        try:
            from app.engine.kg_evidence import stamp_edges
            if stamp_edges(db, kb_id):
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：边变更 → 图缓存失效
    return {"id": e.id}


@router.post("/edges/{edge_id}/confirm", dependencies=[Depends(require_admin)], summary="校对候选关系（确认参与检索 / 拒绝）",)
def confirm_edge(edge_id: int, accept: bool = True):
    db = SessionLocal()
    try:
        e = db.get(KGEdge, edge_id)
        if not e:
            raise HTTPException(404, "关系不存在")
        e.status = "confirmed" if accept else "rejected"
        kb_id = e.kb_id
        db.commit()
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：边状态变更 → 图缓存失效
    return {"ok": True}


@router.get("/{kb_id}/link", summary="关联检索预览（实体链接→多跳扩展 FR-117）", description="实体链接（别名词典+分词匹配，上下文消歧）→ 多跳扩展（默认 1 跳、节点上限 500）→ 概念同义词扩展；未命中实体时返回空且不影响其他检索路径。",)
def link(kb_id: int, q: str):
    """检索联动预览：实体链接 + 多跳扩展（§8.9）。"""
    ents = KGStore.link_entities(kb_id, q)
    sub = KGStore.expand(kb_id, [e["id"] for e in ents], depth=1) if ents else {"nodes": [], "edges": []}
    db = SessionLocal()
    try:
        nmap = {n.id: n.name for n in db.scalars(select(KGNode).where(KGNode.kb_id == kb_id)).all()}
    finally:
        db.close()
    return {"entities": ents,
            "nodes": [{"id": nid, "name": nmap.get(nid, nid)} for nid in sub["nodes"][:100]],
            "edges": [{**e, "src_name": nmap.get(e["src"], e["src"]),
                       "dst_name": nmap.get(e["dst"], e["dst"])} for e in sub["edges"][:100]]}


@router.get("/{kb_id}/export", summary="导出三元组（JSON）",)
def export_kg(kb_id: int):
    db = SessionLocal()
    try:
        nodes = db.scalars(select(KGNode).where(KGNode.kb_id == kb_id)).all()
        edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id)).all()
        concepts = db.scalars(select(KGConcept).where(KGConcept.kb_id == kb_id)).all()
    finally:
        db.close()
    import io
    buf = io.StringIO()
    buf.write(json.dumps({"nodes": [{"id": n.id, "name": n.name, "concept_id": n.concept_id,
                                     "aliases": _pl(n.aliases), "status": n.status} for n in nodes],
                          "edges": [{"src_id": e.src_id, "dst_id": e.dst_id, "relation": e.relation,
                                     "status": e.status} for e in edges],
                          "concepts": [{"id": c.id, "name": c.name, "parent_id": c.parent_id,
                                        "synonyms": _pl(c.synonyms)} for c in concepts]},
                         ensure_ascii=False, indent=2))
    from fastapi.responses import Response
    audit("kg.export", "kb", kb_id, detail={"nodes": len(nodes), "edges": len(edges)})   # S-09：批量导出补审计
    return Response(buf.getvalue(), media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename=kg_{kb_id}.json"})


EXTRACT_PROMPT = """你是知识图谱构建专家。从以下知识库片段中抽取实体与关系。
要求：
1. 实体 type 限定为：人员/系统/产品/流程/制度/部门/概念/事件（事件指有参与者或时间的活动，如 审批/故障/发布）。
2. 关系必须有明确谓词（如 依赖/负责/包含/审批/适用于/部署于），并标注 evidence（来源片段编号）。
3. 只依据片段内容抽取，不要臆造；没有可抽取内容时输出空数组。
4. 输出严格 JSON（不要任何解释文字）：
{"entities":[{"name":"...","type":"...","aliases":[]}],"relations":[{"src":"...","dst":"...","relation":"...","evidence":1}]}
片段：
{blocks}"""

# M3 重构：提示词/解析/概念建档已下沉至 engine/kge.py，此处保留别名供路由兼容
from app.engine.kge import parse_extract_json as _parse_extract_json, ensure_concept as _ensure_concept


class ExtractIn(BaseModel):
    doc_ids: list[int] = []   # 空=全库
    max_chunks: int = 30


@router.post("/{kb_id}/extract", dependencies=[Depends(require_admin)],
              summary="从文档自动抽取实体与关系（LLM，候选态，人工确认后参与检索）")
async def extract(kb_id: int, body: ExtractIn):
    """Schema 约束抽取（FR-121）：取知识库片段→LLM 抽取→候选节点/边入库（带证据 chunk）。"""
    db = SessionLocal()
    try:
        conds = [ChunkMeta.kb_id == kb_id, ChunkMeta.role == "child"]
        if body.doc_ids:
            from app.models import DocVersion as _DV
            vids = db.scalars(select(_DV.id).where(_DV.document_id.in_(body.doc_ids))).all()
            conds.append(ChunkMeta.doc_version_id.in_(vids))
        chunks = db.scalars(select(ChunkMeta).where(*conds).limit(max(1, body.max_chunks))).all()
    finally:
        db.close()
    if not chunks:
        return {"entities": 0, "relations": 0, "chunks": 0, "note": "无可用片段，请先完成文档入库"}

    blocks = "\n".join(f"[{c.id}] {(c.text or '')[:600]}" for c in chunks)
    from app.providers.llm import get_llm
    from app.core.metering import record_usage, audit
    llm = get_llm()
    try:
        raw, usage = await llm.chat([
            {"role": "system", "content": "只输出 JSON，不要任何解释文字。"},
            {"role": "user", "content": EXTRACT_PROMPT.replace("{blocks}", blocks)},
        ], temperature=0, max_tokens=2048)
    except Exception as e:
        return {"entities": 0, "relations": 0, "chunks": len(chunks), "error": f"LLM 调用失败: {e}"}
    if usage:
        record_usage(llm.model_id, getattr(llm, "vendor", llm.name), "extract", kb_id,
                     usage.prompt_tokens, usage.completion_tokens, usage.is_estimated)
    try:
        data = _parse_extract_json(raw)
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().warning("KG 抽取 LLM 输出非 JSON | kb=%s | raw=%s", kb_id, (raw or "")[:300])
        return {"entities": 0, "relations": 0, "chunks": len(chunks), "error": "LLM 输出非 JSON，请重试或更换模型"}

    valid_chunk_ids = {c.id for c in chunks}
    db = SessionLocal()
    ne, nr = 0, 0
    try:
        ne, nr = _apply_extract_to_graph(db, kb_id, data, valid_chunk_ids)
        db.commit()
    except Exception as e:
        db.rollback()
        return {"entities": 0, "relations": 0, "chunks": len(chunks), "error": str(e)[:200]}
    finally:
        db.close()
    audit("kg.extract", "kb", kb_id, detail={"entities": ne, "relations": nr})
    return {"entities": ne, "relations": nr, "chunks": len(chunks)}


def _apply_extract_to_graph(db, kb_id: int, data: dict, valid_chunk_ids: set) -> tuple[int, int]:
    """抽取结果入图（候选态）：手动抽取与变更驱动候选确认（M3）共用。

    不提交事务，由调用方控制提交时机；返回 (新增实体数, 新增关系数)。
    """
    ne, nr = 0, 0
    existing = {n.name: n for n in db.scalars(select(KGNode).where(KGNode.kb_id == kb_id)).all()}
    id_by_name: dict[str, int] = {name: n.id for name, n in existing.items()}
    for e in (data.get("entities") or [])[:60]:
        name = str(e.get("name", "")).strip()[:128]
        if not name:
            continue
        if name not in id_by_name:
            node = KGNode(kb_id=kb_id, name=name, concept_id=_ensure_concept(db, kb_id, str(e.get("type", ""))),
                          aliases=json.dumps(e.get("aliases") or [], ensure_ascii=False), status="candidate")
            db.add(node)
            db.flush()
            ne += 1
        id_by_name.setdefault(name, db.scalar(select(KGNode.id).where(
            KGNode.kb_id == kb_id, KGNode.name == name)))
    for r in (data.get("relations") or [])[:100]:
        src = id_by_name.get(str(r.get("src", "")).strip())
        dst = id_by_name.get(str(r.get("dst", "")).strip())
        if not src or not dst or src == dst:
            continue
        ev = r.get("evidence")
        ev_id = ev if isinstance(ev, int) and ev in valid_chunk_ids else None
        db.add(KGEdge(kb_id=kb_id, src_id=src, dst_id=dst,
                      relation=str(r.get("relation", "相关"))[:64],
                      evidence_chunk_id=ev_id, status="candidate"))
        nr += 1
    # A-07：写入即回填证据指纹（clean_hash 稳定指纹，文档重建后可重映射），调用方提交
    from app.engine.kg_evidence import stamp_edges
    stamp_edges(db, kb_id, valid_chunk_ids)
    return ne, nr


class BatchIn(BaseModel):
    ids: list[int]
    action: str          # confirm | reject


def _apply_batch(db, kb_id: int, ids: list[int], action: str, kind: str) -> int:
    """候选批量处理：confirm 的关系自动级联确认其两端候选实体。"""
    status = "confirmed" if action == "confirm" else "rejected"
    n = 0
    if kind == "edge":
        for e in db.scalars(select(KGEdge).where(KGEdge.id.in_(ids), KGEdge.kb_id == kb_id)).all():
            e.status = status
            n += 1
            if status == "confirmed":
                for nid in (e.src_id, e.dst_id):
                    node = db.get(KGNode, nid)
                    if node and node.status == "candidate":
                        node.status = "confirmed"
    else:
        for node in db.scalars(select(KGNode).where(KGNode.id.in_(ids), KGNode.kb_id == kb_id)).all():
            node.status = status
            n += 1
    db.commit()
    return n


@router.post("/{kb_id}/edges/batch", dependencies=[Depends(require_admin)],
              summary="候选关系批量确认/拒绝（确认时级联确认两端候选实体）")
def edges_batch(kb_id: int, body: BatchIn):
    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, "action 须为 confirm 或 reject")
    db = SessionLocal()
    try:
        n = _apply_batch(db, kb_id, body.ids, body.action, "edge")
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：批量确认/拒绝 → 图缓存失效
    audit("kg.edges_batch", "kb", kb_id, detail={"action": body.action, "affected": n})
    return {"affected": n}


@router.post("/{kb_id}/nodes/batch", dependencies=[Depends(require_admin)],
              summary="候选实体批量确认/拒绝")
def nodes_batch(kb_id: int, body: BatchIn):
    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, "action 须为 confirm 或 reject")
    db = SessionLocal()
    try:
        n = _apply_batch(db, kb_id, body.ids, body.action, "node")
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：批量确认/拒绝 → 图缓存与实体向量缓存失效
    audit("kg.nodes_batch", "kb", kb_id, detail={"action": body.action, "affected": n})
    return {"affected": n}


@router.post("/{kb_id}/candidates/confirm-all", dependencies=[Depends(require_admin)],
              summary="一键确认全部候选（关系+实体，关系级联确认两端实体）")
def confirm_all(kb_id: int):
    db = SessionLocal()
    try:
        ne = _apply_batch(db, kb_id, [e.id for e in db.scalars(
            select(KGEdge).where(KGEdge.kb_id == kb_id, KGEdge.status == "candidate")).all()], "confirm", "edge")
        nn = _apply_batch(db, kb_id, [n.id for n in db.scalars(
            select(KGNode).where(KGNode.kb_id == kb_id, KGNode.status == "candidate")).all()], "confirm", "node")
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：一键确认 → 图缓存与实体向量缓存失效
    audit("kg.confirm_all", "kb", kb_id, detail={"edges": ne, "nodes": nn})
    return {"edges_confirmed": ne, "nodes_confirmed": nn}


@router.get("/{kb_id}/coverage", summary="图谱覆盖率提示（入库文档 vs 图谱规模）")
def coverage(kb_id: int):
    db = SessionLocal()
    try:
        from app.models import Document
        docs = db.scalar(select(func.count()).select_from(Document).where(
            Document.kb_id == kb_id, Document.status == "published")) or 0
        nodes = db.scalar(select(func.count()).select_from(KGNode).where(KGNode.kb_id == kb_id)) or 0
        edges = db.scalar(select(func.count()).select_from(KGEdge).where(KGEdge.kb_id == kb_id)) or 0
        confirmed = db.scalar(select(func.count()).select_from(KGEdge).where(
            KGEdge.kb_id == kb_id, KGEdge.status == "confirmed")) or 0
        candidates = edges - confirmed
    finally:
        db.close()
    tip = "图谱为空：先完成文档入库，再点「AI 抽取」"
    if docs and nodes == 0:
        tip = f"已有 {docs} 篇文档入库但尚未抽取，点「AI 抽取」开始构建"
    elif candidates > 0:
        tip = f"有 {candidates} 条候选关系待确认，确认后才参与关联检索"
    elif docs and confirmed > 0 and docs > 1:
        tip = "注意：仅部分文档参与过抽取，新入库文档请再次抽取"
    return {"docs": int(docs), "nodes": int(nodes), "edges": int(edges),
            "confirmed_edges": int(confirmed), "candidates": int(candidates), "tip": tip}


@router.get("/evidence/{chunk_id}", summary="关系证据原文（chunk 文本+所属文档）")
def evidence(chunk_id: int):
    from app.models import ChunkMeta, Document
    db = SessionLocal()
    try:
        c = db.get(ChunkMeta, chunk_id)
        if not c:
            raise HTTPException(404, "片段不存在")
        doc = db.get(Document, (db.scalar(select(DocVersion.document_id).where(DocVersion.id == c.doc_version_id)) or 0))
    finally:
        db.close()
    return {"chunk_id": c.id, "text": c.text, "page": c.page, "heading": c.heading_path,
            "doc_title": doc.title if doc else "（文档已删除）", "doc_id": doc.id if doc else None}


@router.post("/{kb_id}/prune-dangling", dependencies=[Depends(require_admin)],
              summary="清理悬空证据边（证据 chunk 已不存在的候选/确认边）")
def prune_dangling(kb_id: int):
    db = SessionLocal()
    try:
        chunk_ids = set(db.scalars(select(ChunkMeta.id).where(ChunkMeta.kb_id == kb_id)).all())
        edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id)).all()
        dead = [e for e in edges if e.evidence_chunk_id and e.evidence_chunk_id not in chunk_ids]
        for e in dead:
            db.delete(e)
        db.commit()
    finally:
        db.close()
    _invalidate_kg(kb_id)   # B-07：删边 → 图缓存失效
    audit("kg.prune_dangling", "kb", kb_id, detail={"removed": len(dead)})
    return {"removed": len(dead)}


@router.get("/{kb_id}/health", summary="图谱健康体检（孤立实体/悬空证据/候选积压/未覆盖文档）")
def health(kb_id: int):
    """知识质量自动诊断（借鉴 kp_wiki lint 思想）：系统自己找问题而非等用户发现。"""
    db = SessionLocal()
    try:
        nodes = db.scalars(select(KGNode).where(KGNode.kb_id == kb_id)).all()
        edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id)).all()
        node_ids = {n.id for n in nodes}
        issues = []
        orphans = [n for n in nodes if not any(e.src_id == n.id or e.dst_id == n.id for e in edges)]
        if orphans:
            issues.append({"type": "孤立实体", "severity": "中",
                           "detail": f"{len(orphans)} 个实体没有任何关系：{', '.join(n.name for n in orphans[:5])}",
                           "names": [n.name for n in orphans]})
        chunk_ids = set(db.scalars(select(ChunkMeta.id).where(ChunkMeta.kb_id == kb_id)).all())
        dangling = [e for e in edges if e.evidence_chunk_id and e.evidence_chunk_id not in chunk_ids]
        if dangling:
            issues.append({"type": "悬空证据", "severity": "高",
                           "detail": f"{len(dangling)} 条关系的证据片段已不存在（文档被删除/重建），建议重新抽取或删除",
                           "ids": [e.id for e in dangling]})
        cands = sum(1 for e in edges if e.status == "candidate") + sum(1 for n in nodes if n.status == "candidate")
        if cands:
            issues.append({"type": "候选积压", "severity": "低",
                           "detail": f"{cands} 条候选待确认（确认后才参与关联检索）"})
        from app.models import Document as _D, DocVersion as _DV
        docs = db.scalars(select(_D).where(_D.kb_id == kb_id, _D.status == "published")).all()
        doc_ids = {d.id for d in docs}
        covered = set()
        from app.models import ChunkMeta as _CM
        ev_ids = [e.evidence_chunk_id for e in edges
                  if e.evidence_chunk_id and e.evidence_chunk_id in chunk_ids]
        if ev_ids:
            for c in db.scalars(select(_CM).where(_CM.id.in_(ev_ids))).all():
                dv = db.get(_DV, c.doc_version_id)
                if dv:
                    covered.add(dv.document_id)
        uncovered = doc_ids - covered
        if len(docs) > 1 and len(uncovered) > 0:
            issues.append({"type": "未覆盖文档", "severity": "中",
                           "detail": f"{len(uncovered)}/{len(docs)} 篇已发布文档从未被抽取进图谱，相关实体关系缺失"})
    finally:
        db.close()
    return {"healthy": not issues, "issues": issues}


@router.get("/{kb_id}/insights", summary="图谱洞察（枢纽实体/社区聚类/孤立节点）")
def insights(kb_id: int):
    import networkx as nx
    from app.providers.kg_store import KGStore
    g = KGStore.build_graph(kb_id, confirmed_only=True)
    if g.number_of_nodes() == 0:
        return {"hubs": [], "communities": [], "orphans": [], "note": "无已确认图谱数据"}
    degree = sorted(g.degree, key=lambda x: -x[1])[:5]
    hubs = [{"name": g.nodes[n]["name"], "degree": d} for n, d in degree if d > 0]
    from networkx.algorithms.community import greedy_modularity_communities
    try:
        comms = list(greedy_modularity_communities(g.to_undirected()))
    except Exception:
        comms = []
    communities = [{"size": len(c), "names": [g.nodes[n]["name"] for n in list(c)[:8]]}
                   for c in sorted(comms, key=len, reverse=True)[:5] if len(c) >= 2]
    orphans = [g.nodes[n]["name"] for n, d in g.degree if d == 0]
    return {"hubs": hubs, "communities": communities, "orphans": orphans}


# ---------------------------------------------------------------------------
# M3 变更驱动抽取候选队列（升级迭代方案 §4.3）：发布自动抽取 → 待审 → 确认入图
# ---------------------------------------------------------------------------
@router.get("/{kb_id}/extract-candidates", summary="抽取候选列表（M3：wiki 发布自动入队，待人工确认）")
def extract_candidates(kb_id: int, status: str | None = None, page: int = 1, size: int = 20):
    from app.models import Document, KgCandidate
    db = SessionLocal()
    try:
        conds = [KgCandidate.kb_id == kb_id] + ([KgCandidate.status == status] if status else [])
        total = db.scalar(select(func.count()).select_from(KgCandidate).where(*conds)) or 0
        rows = db.scalars(select(KgCandidate).where(*conds).order_by(KgCandidate.id.desc())
                          .offset((page - 1) * size).limit(size)).all()
        tmap = {d.id: d.title for d in db.scalars(select(Document).where(
            Document.kb_id == kb_id)).all()}
        items = []
        for c in rows:
            p = json.loads(c.payload or "{}")
            items.append({"id": c.id, "doc_id": c.doc_id, "doc_title": tmap.get(c.doc_id, f"文档{c.doc_id}"),
                          "doc_version_id": c.doc_version_id, "status": c.status,
                          "entities": len(p.get("entities") or []), "relations": len(p.get("relations") or []),
                          "created_at": str(c.created_at or ""), "reviewed_at": str(c.reviewed_at or "")})
        return {"total": total, "items": items}
    finally:
        db.close()


@router.get("/extract-candidates/{candidate_id}", summary="抽取候选详情（实体/关系明细预览）")
def extract_candidate_detail(candidate_id: int):
    from app.models import KgCandidate
    db = SessionLocal()
    try:
        c = db.get(KgCandidate, candidate_id)
        if not c:
            raise HTTPException(404, "候选不存在")
        p = json.loads(c.payload or "{}")
        return {"id": c.id, "doc_id": c.doc_id, "status": c.status, "payload": p,
                "created_at": str(c.created_at or ""), "reviewed_at": str(c.reviewed_at or "")}
    finally:
        db.close()


@router.post("/extract-candidates/{candidate_id}/confirm", dependencies=[Depends(require_admin)],
              summary="确认抽取候选（写入图谱候选态，再走既有 /edges/batch 最终确认）")
def extract_candidate_confirm(candidate_id: int):
    from app.engine.wiki_extract import confirm_candidate
    r = confirm_candidate(candidate_id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "确认失败"))
    return r


@router.post("/extract-candidates/{candidate_id}/reject", dependencies=[Depends(require_admin)],
              summary="拒绝抽取候选（不入图，留档审计）")
def extract_candidate_reject(candidate_id: int):
    from app.engine.wiki_extract import reject_candidate
    r = reject_candidate(candidate_id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "拒绝失败"))
    return r


@router.post("/{kb_id}/extract-candidates/{doc_id}/trigger", dependencies=[Depends(require_admin)],
              summary="手动触发单文档变更驱动抽取（补跑/重试）")
async def extract_candidate_trigger(kb_id: int, doc_id: int):
    from app.engine.wiki_extract import extract_doc_to_candidate
    r = await extract_doc_to_candidate(doc_id)
    return r
