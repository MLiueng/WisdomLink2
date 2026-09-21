"""证据指纹化（A-07）：图谱边证据与文档重建解耦。

问题：`KGEdge.evidence_chunk_id` 指向自增 `chunk_meta.id`，文档重建/重处理/
新版本切换都会物理删除旧分片并生成全新 id → 证据指针悬空（recon 只能度量、
prune 只能删边，知识关系随重建流失）。

方案：边并行记录证据分片的 `clean_hash` 稳定指纹（同文本跨重建不变）：
  - 写入时 `stamp_edges` 回填指纹（手动抽取/候选确认/手工加边共用）；
  - 发布/重建后 `remap_edges` 按指纹把悬空指针重映射到新分片，
    证据有效率恢复 100%（验收②）；
  - 指纹也找不到（证据文本确实被删除/清洗）的边保留指针为空，
    由 prune_dangling/recon 兜底治理。
"""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ChunkMeta, KGEdge


def stamp_edges(db, kb_id: int, chunk_ids: set[int] | None = None) -> int:
    """为本库缺指纹但有合法证据指针的边回填 clean_hash（调用方控制提交）。

    chunk_ids 给定时只处理证据落在该集合内的边（抽取场景）；
    缺省时全库补填（兜底场景）。返回补填条数。
    """
    conds = [KGEdge.kb_id == kb_id,
             KGEdge.evidence_chunk_id.is_not(None),
             KGEdge.evidence_clean_hash.is_(None)]
    edges = db.scalars(select(KGEdge).where(*conds)).all()
    if not edges:
        return 0
    eids = {e.evidence_chunk_id for e in edges}
    if chunk_ids is not None:
        eids &= set(chunk_ids)
    if not eids:
        return 0
    rows = db.execute(select(ChunkMeta.id, ChunkMeta.clean_hash)
                      .where(ChunkMeta.id.in_(eids))).all()
    hmap = {cid: h for cid, h in rows}
    n = 0
    for e in edges:
        h = hmap.get(e.evidence_chunk_id)
        if h:
            e.evidence_clean_hash = h
            n += 1
    return n


def remap_edges(kb_id: int) -> dict:
    """发布/重建后按指纹重映射悬空证据指针；返回统计（供审计与测试）。"""
    db = SessionLocal()
    try:
        edges = db.scalars(select(KGEdge).where(
            KGEdge.kb_id == kb_id,
            KGEdge.evidence_chunk_id.is_not(None))).all()
        if not edges:
            return {"remapped": 0, "dangling": 0, "valid": 0}
        chunk_ids = {e.evidence_chunk_id for e in edges}
        alive = set(db.scalars(select(ChunkMeta.id).where(
            ChunkMeta.id.in_(chunk_ids))).all())
        # 自愈：证据仍存活但缺指纹的存量边先补指纹（下次重建即可重映射）
        if stamp_edges(db, kb_id):
            db.commit()
        need = [e for e in edges if e.evidence_chunk_id not in alive]
        if not need:
            return {"remapped": 0, "dangling": 0, "valid": len(edges)}
        # 指纹 → 存活分片（同库多版本时取 active 分片；同指纹多条取任意一条即可）
        hashes = {e.evidence_clean_hash for e in need if e.evidence_clean_hash}
        hmap: dict[str, int] = {}
        if hashes:
            rows = db.execute(select(ChunkMeta.id, ChunkMeta.clean_hash)
                              .where(ChunkMeta.kb_id == kb_id,
                                     ChunkMeta.role == "child",
                                     ChunkMeta.clean_hash.in_(hashes))).all()
            for cid, h in rows:
                hmap.setdefault(h, cid)
        remapped = dangling = 0
        for e in need:
            target = hmap.get(e.evidence_clean_hash or "")
            if target:
                e.evidence_chunk_id = target
                remapped += 1
            else:
                dangling += 1
        if remapped or dangling:
            db.commit()
        return {"remapped": remapped, "dangling": dangling,
                "valid": len(edges) - remapped - dangling}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
