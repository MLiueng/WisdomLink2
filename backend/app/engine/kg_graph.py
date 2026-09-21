"""图谱缓存与实体向量索引（B-07）：替代每请求全量重建图/全表扫节点。

三层缓存（均带 TTL + 主动失效双保险）：
  1. 图缓存 `graph_cache(kb_id)`：confirmed 节点/边构建的 NetworkX 图 +
     分片倒排索引（B-08：jieba 词元→chunk_id 集合，替代文本 LIKE 全表扫）；
     指纹校验（confirmed 节点/边计数变化或 TTL 到期即重建）。
  2. 实体向量索引 `_node_vec_cache`：confirmed 节点（名称+别名）嵌入缓存
     （B-04 embed_with_cache 复用，purpose=kg_node），支持查询向量近邻实体链接。
  3. 失效钩子 `invalidate_graph_cache/invalidate_node_vecs`：图谱写操作
     （节点/边增删、状态变更、候选确认、文档重建）后主动调用。

灰度纪律：向量实体链接由 `sys_config.runtime_kg_vector_link`（默认 off）控制，
关闭时退化为原有字符串匹配链接（回退路径保留）。
"""
import time
from typing import Any

import networkx as nx
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ChunkMeta, KGEdge, KGNode

_TTL = 300.0          # 缓存兜底过期（秒），指纹/主动失效为主
_graph_cache: dict[int, dict[str, Any]] = {}
_node_vec_cache: dict[int, dict[str, Any]] = {}


def invalidate_graph_cache(kb_id: int | None = None) -> None:
    """图谱写操作后调用：指定库失效，None 表示全库失效（幂等）。"""
    if kb_id is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(kb_id, None)


def invalidate_node_vecs(kb_id: int | None = None) -> None:
    """节点增删/名称变更后调用（幂等）。"""
    if kb_id is None:
        _node_vec_cache.clear()
    else:
        _node_vec_cache.pop(kb_id, None)


def _fingerprint(db, kb_id: int) -> tuple[int, int]:
    """轻量指纹：confirmed 节点/边计数（任一变化即缓存过期）。"""
    n = db.scalar(select(func.count()).select_from(KGNode)
                  .where(KGNode.kb_id == kb_id, KGNode.status == "confirmed")) or 0
    e = db.scalar(select(func.count()).select_from(KGEdge)
                  .where(KGEdge.kb_id == kb_id, KGEdge.status == "confirmed")) or 0
    return int(n), int(e)


def graph_cache(kb_id: int) -> dict[str, Any]:
    """取（或按需重建）库级图缓存：{"graph", "chunk_index", "by_key"}。

    graph：confirmed 节点/边的 NetworkX 有向图（边带 evidence_chunk_id/evidence_clean_hash）；
    chunk_index：库内 child 分片 id 集合；
    by_key（B-08）：jieba 词元 → chunk_id 集合 倒排。
    """
    now = time.monotonic()
    db = SessionLocal()
    try:
        fp = _fingerprint(db, kb_id)
        ent = _graph_cache.get(kb_id)
        if ent and ent["fingerprint"] == fp and now - ent["ts"] < _TTL:
            return ent
        nodes = db.scalars(select(KGNode).where(KGNode.kb_id == kb_id,
                                                KGNode.status == "confirmed")).all()
        edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id,
                                                KGEdge.status == "confirmed")).all()
        chunks = db.execute(select(ChunkMeta.id, ChunkMeta.clean_hash, ChunkMeta.text)
                            .where(ChunkMeta.kb_id == kb_id,
                                   ChunkMeta.role == "child")).all()
    finally:
        db.close()
    g = nx.DiGraph()
    for n in nodes:
        g.add_node(n.id, name=n.name, concept_id=n.concept_id, aliases=_safe_list(n.aliases))
    for e in edges:
        if e.src_id in g and e.dst_id in g:
            g.add_edge(e.src_id, e.dst_id, relation=e.relation,
                       evidence_chunk_id=e.evidence_chunk_id,
                       evidence_clean_hash=getattr(e, "evidence_clean_hash", None))
    # B-08 实体倒排：词元→分片集合（与 BM25 分词同口径），替代文本 LIKE 扫描
    from app.engine.retrieval import _tokenize
    by_key: dict[str, set[int]] = {}
    chunk_ids: set[int] = set()
    for cid, _ch, text in chunks:
        chunk_ids.add(cid)
        for tk in set(_tokenize(text or "")):
            by_key.setdefault(tk, set()).add(cid)
    ent = {"graph": g, "chunk_index": chunk_ids, "by_key": by_key,
           "fingerprint": fp, "ts": now}
    _graph_cache[kb_id] = ent
    return ent


def vector_link_enabled() -> bool:
    """B-07 灰度开关：`runtime_kg_vector_link=on` 启用向量实体链接（默认关闭）。"""
    from app.core.runtime_config import get_runtime
    return (get_runtime("runtime_kg_vector_link") or "off") == "on"


def _confirmed_nodes_sync(kb_id: int) -> list:
    """同步段：confirmed 节点读取（P-02：经 to_thread 卸载，不在事件循环执行）。"""
    db = SessionLocal()
    try:
        return db.scalars(select(KGNode).where(KGNode.kb_id == kb_id,
                                               KGNode.status == "confirmed")).all()
    finally:
        db.close()


def _link_names_sync(ids: list[int]) -> dict[int, tuple[str, Any]]:
    """同步段：命中节点名称/概念回捞（P-02 卸载）。"""
    db = SessionLocal()
    try:
        nodes = db.scalars(select(KGNode).where(KGNode.id.in_(ids))).all()
        return {n.id: (n.name, n.concept_id) for n in nodes}
    finally:
        db.close()


async def _node_vectors(kb_id: int) -> dict[int, list[float]]:
    """confirmed 节点向量（名称+别名文本嵌入，B-04 缓存复用；TTL 兜底）。

    P-02：节点读取同步段经 to_thread 卸载（嵌入缓存 DB 段同理）。
    """
    import asyncio
    now = time.monotonic()
    ent = _node_vec_cache.get(kb_id)
    if ent and now - ent["ts"] < _TTL:
        return ent["vecs"]
    nodes = await asyncio.to_thread(_confirmed_nodes_sync, kb_id)
    vecs: dict[int, list[float]] = {}
    if nodes:
        from app.engine.embed_cache import embed_with_cache
        from app.providers.embedding import get_embedding
        texts, ids = [], []
        for n in nodes:
            aliases = _safe_list(n.aliases)
            texts.append(n.name + ("；" + "；".join(aliases) if aliases else ""))
            ids.append(n.id)
        vs = await embed_with_cache(get_embedding(), texts, purpose="kg_node", kb_id=kb_id)
        vecs = dict(zip(ids, vs))
    _node_vec_cache[kb_id] = {"vecs": vecs, "ts": now}
    return vecs


async def vector_link(kb_id: int, qvec: list[float], top_k: int = 8,
                      threshold: float | None = None) -> list[dict]:
    """B-07 向量实体链接：查询向量与实体向量近邻（阈值过滤，余弦取 top_k）。

    返回 [{"id","name","concept_id"}]，与字符串链接同结构（调用方可直接合并）。
    """
    import asyncio
    if not qvec:
        return []
    if not vector_link_enabled():   # 灰度纪律在能力层自守：关闭时任何调用方都拿不到结果
        return []
    from app.config import get_settings
    from app.providers.embedding import cosine
    thr = threshold if threshold is not None else get_settings().kg_link_sim_threshold
    vecs = await _node_vectors(kb_id)
    if not vecs:
        return []
    # P-02：全量余弦打分为 CPU 段，节点量大时卸载线程池
    scored = await asyncio.to_thread(
        lambda: sorted(((nid, cosine(qvec, v)) for nid, v in vecs.items() if v),
                       key=lambda x: x[1], reverse=True))
    hits = [(nid, s) for nid, s in scored[:top_k] if s >= thr]
    if not hits:
        return []
    nm = await asyncio.to_thread(_link_names_sync, [nid for nid, _ in hits])
    return [{"id": nid, "name": nm[nid][0], "concept_id": nm[nid][1]}
            for nid, _ in hits if nid in nm]


def _safe_list(raw: str) -> list:
    try:
        import json
        return json.loads(raw or "[]")
    except Exception:
        return []
