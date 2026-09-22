"""KGStore Provider（ADR-006）：MySQL 概念/实体/边三表 + NetworkX 内存图多跳扩展。"""
import networkx as nx
from sqlalchemy import select
from app.db import SessionLocal
from app.models import KGConcept, KGEdge, KGNode


class KGStore:
    @staticmethod
    def build_graph(kb_id: int, confirmed_only: bool = True) -> nx.DiGraph:
        db = SessionLocal()
        try:
            q = select(KGNode).where(KGNode.kb_id == kb_id)
            if confirmed_only:
                q = q.where(KGNode.status == "confirmed")
            nodes = db.scalars(q).all()
            edges = db.scalars(select(KGEdge).where(KGEdge.kb_id == kb_id,
                                                    KGEdge.status == "confirmed" if confirmed_only else True)).all()
        finally:
            db.close()
        g = nx.DiGraph()
        for n in nodes:
            g.add_node(n.id, name=n.name, concept_id=n.concept_id)
        for e in edges:
            if e.src_id in g and e.dst_id in g:
                g.add_edge(e.src_id, e.dst_id, relation=e.relation, evidence_chunk_id=e.evidence_chunk_id)
        return g

    @staticmethod
    def expand(kb_id: int, node_ids: list[int], depth: int = 1, max_nodes: int = 500) -> dict:
        """§8.9：多跳扩展（默认 1 跳，可配 ≤2；单次扩展节点上限 500）。"""
        return KGStore.expand_on_graph(KGStore.build_graph(kb_id), node_ids, depth, max_nodes)

    @staticmethod
    def expand_on_graph(g: nx.DiGraph, node_ids: list[int], depth: int = 1,
                        max_nodes: int = 500) -> dict:
        """多跳扩展（B-07）：在给定图上扩展，供图缓存复用（不再每次重建图）。"""
        frontier = set(n for n in node_ids if n in g)
        visited = set(frontier)
        edges_found: list[dict] = []
        for _ in range(max(1, depth)):
            nxt: set[int] = set()
            for n in frontier:
                # 出边 n→dst 与入边 src→n 分别记录（原实现把入边记成自环 {"src": n, "dst": n}）
                discovered: list[int] = []
                for _, dst, data in g.out_edges(n, data=True):
                    edges_found.append({"src": n, "dst": dst, **data})
                    discovered.append(dst)
                for src, _, data in g.in_edges(n, data=True):
                    edges_found.append({"src": src, "dst": n, **data})
                    discovered.append(src)
                for x in discovered:
                    if x not in visited and len(visited) < max_nodes:
                        visited.add(x)
                        nxt.add(x)
            frontier = nxt - visited
            if not frontier or len(visited) >= max_nodes:
                break
        return {"nodes": list(visited), "edges": edges_found[:1000]}

    @staticmethod
    def link_entities(kb_id: int, query: str) -> list[dict]:
        """实体链接：别名/名称子串匹配 + jieba 分词全等匹配（轻量版，生产可升级）。"""
        db = SessionLocal()
        try:
            nodes = db.scalars(select(KGNode).where(KGNode.kb_id == kb_id, KGNode.status == "confirmed")).all()
        finally:
            db.close()
        import jieba
        tokens = set(jieba.lcut(query.lower()))
        hits: list[dict] = []
        for n in nodes:
            names = [n.name.lower()] + [a.lower() for a in _safe_list(n.aliases)]
            for nm in names:
                if (nm and nm in tokens) or (len(nm) >= 2 and nm in query.lower()):
                    hits.append({"id": n.id, "name": n.name, "concept_id": n.concept_id})
                    break
        return hits

    @staticmethod
    def concept_expand(kb_id: int, concept_ids: list[int]) -> list[str]:
        """概念扩展：同义词 + 下位概念名并入查询改写。"""
        db = SessionLocal()
        try:
            concepts = db.scalars(select(KGConcept).where(KGConcept.kb_id == kb_id)).all()
        finally:
            db.close()
        by_parent: dict[int | None, list] = {}
        for c in concepts:
            by_parent.setdefault(c.parent_id, []).append(c)
        out: set[str] = set()
        stack = list(concept_ids)
        while stack:
            cid = stack.pop()
            for c in by_parent.get(cid, []):
                for syn in _safe_list(c.synonyms):
                    out.add(syn)
                out.add(c.name)
                stack.append(c.id)
        return list(out)[:20]


def _safe_list(raw: str) -> list:
    try:
        import json
        return json.loads(raw or "[]")
    except Exception:
        return []
