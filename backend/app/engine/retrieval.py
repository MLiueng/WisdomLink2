"""检索引擎（FR-106/117/123）：三路召回（稠密+BM25+图谱）→ RRF 融合（BR-007）→ 重排 → 父子富化。

召回优化（2026-09-18 检索专项）：
  - BM25 分词升级：jieba 搜索模式（lcut_for_search）产出细粒度子词，提升中文长复合词召回；
  - 停用词过滤：高频虚词不参与打分，避免 IDF 稀释与噪声命中；
  - 召回深度提升：各路 top-20 → top-25，扩大融合候选池；
  - 联网检索补充：本地召回不足时以 web 路补充外部知识（灰度开关控制）。
"""
import asyncio
import re
import time
from sqlalchemy import select
from app.db import SessionLocal
from app.models import ChunkMeta, DocVersion, Document
from app.core.rules import rrf_fuse_weighted
from app.providers.base import ChunkHit
from app.providers.embedding import cosine, get_embedding
from app.providers.kg_store import KGStore
from app.providers.rerank import get_reranker
from app.providers.vector_store import get_vector_store

_bm25_cache: dict[int, tuple[float, object, list[int]]] = {}  # kb_id -> (ts, index, chunk_ids)

# 中文高频停用词（精简集）：不参与 BM25 打分，防止 IDF 稀释与噪声命中
_STOPWORDS = frozenset("的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
                       "自己 这 他 她 它 们 那 些 什么 怎么 如何 吗 呢 吧 啊 呀 哦 嗯 与 及 或 等 被 把 "
                       "对 从 向 于 为 以 之 其 该 此 这个 那个 请问 能否 是否 需要 进行 使用 可以".split())
# 纯符号/单字母/纯数字短词过滤（BM25 无区分度）
_TOKEN_DROP = re.compile(r"^[\W_]*$")


def _tokenize(text: str) -> list[str]:
    """BM25 分词（召回优化）：搜索模式细粒度切分 + 停用词/噪声词过滤。"""
    import jieba
    return [t for t in jieba.lcut_for_search(text.lower())
            if t.strip() and t not in _STOPWORDS and not _TOKEN_DROP.match(t)]



def invalidate_bm25(kb_id: int) -> None:
    """BM25 索引失效（删除/重建/发布后调用，避免 300s 陈旧窗口内召回已删内容）。"""
    _bm25_cache.pop(kb_id, None)


def _flt(kb_ids: list[int], folder_ids: list[int] | None) -> dict:
    f: dict = {"in": {"kb_id": kb_ids}}
    if folder_ids:
        f["in"]["folder_id"] = folder_ids
    return f


def _live_version_ids(db, kb_id: int) -> set[int]:
    """当前库内"可检索版本"集合：文档已发布/下线（在架）+ 版本为当前版。

    过滤 deleted（删除立即退出召回，H1）与旧版本分片（NFR-263 新旧不混检，H2）。
    offline 文档保留在召回内（既有语义，最小改动）。
    """
    ok_status = ("published", "offline")
    rows = db.execute(select(DocVersion.id)
                      .join(Document, DocVersion.document_id == Document.id)
                      .where(Document.kb_id == kb_id, Document.status.in_(ok_status),
                             DocVersion.is_current == True)).all()  # noqa: E712
    return {r[0] for r in rows}


def _live_versions_map(kb_ids: list[int]) -> dict[int, set[int]]:
    """P-06：入口一次算齐各库可检索版本集合，透传给各路召回。

    此前每请求在 BM25/KG 路内重复计算 2-3 遍（改写轮再放大），统一收敛为单次查询。
    """
    db = SessionLocal()
    try:
        return {kb: _live_version_ids(db, kb) for kb in kb_ids}
    finally:
        db.close()


def _live_versions_one(kb_id: int) -> set[int]:
    """单库版本集合（独立会话）：KG 路缺 live_map 时的自算路径（供 to_thread 卸载）。"""
    db = SessionLocal()
    try:
        return _live_version_ids(db, kb_id)
    finally:
        db.close()


async def dense_recall(kb_ids, vector, top_k=25, folder_ids=None) -> list[ChunkHit]:
    vs = get_vector_store()
    hits: list[ChunkHit] = []
    for kb in kb_ids:
        hits.extend(await vs.search(f"chunk_{kb}", vector, top_k, _flt([kb], folder_ids)))
    return hits


def bm25_recall(kb_ids: list[int], query: str, top_k: int = 25, folder_ids=None,
                live_map: dict[int, set[int]] | None = None) -> list[ChunkHit]:
    """BM25 路（中文 jieba 分词，B-14）：每库内存索引懒加载（300s 失效）。

    live_map（P-06）：入口透传的可检索版本集合，缺省时路内自算（向后兼容）。
    """
    from rank_bm25 import BM25Okapi
    now = time.time()
    hits: list[ChunkHit] = []
    db = SessionLocal()
    try:
        for kb in kb_ids:
            cached = _bm25_cache.get(kb)
            if not cached or now - cached[0] > 300:
                live = live_map.get(kb, set()) if live_map is not None else _live_version_ids(db, kb)
                rows = db.scalars(select(ChunkMeta).where(
                    ChunkMeta.kb_id == kb, ChunkMeta.role == "child",
                    ChunkMeta.doc_version_id.in_(live or [-1]))).all()
                corpus = [_tokenize(r.text) for r in rows]
                index = BM25Okapi(corpus) if corpus else None
                _bm25_cache[kb] = (now, index, [r.id for r in rows])
            _, index, ids = _bm25_cache[kb]
            if not index:
                continue
            scores = index.get_scores(_tokenize(query))
            order = sorted(range(len(scores)), key=lambda i: (-scores[i], ids[i]))[:top_k]
            hit_ids = [ids[i] for i in order if scores[i] > 0]
            if not hit_ids:
                continue
            rows = db.scalars(select(ChunkMeta).where(ChunkMeta.id.in_(hit_ids))).all()
            fmap = {r.id: r for r in rows}
            for i, cid in enumerate(hit_ids):
                r = fmap.get(cid)
                if r and (not folder_ids or r.folder_id in folder_ids):
                    hits.append(ChunkHit(chunk_id=str(r.id), text=r.text, score=float(scores[i]),
                                         kb_id=r.kb_id, folder_id=r.folder_id,
                                         doc_version_id=r.doc_version_id, page=r.page,
                                         heading_path=r.heading_path, source="bm25"))
        return hits
    finally:
        db.close()


def _kg_link(kb_id: int, query: str) -> list[dict]:
    """字符串实体链接（同步段，经 to_thread 卸载，不阻塞事件循环）。"""
    return KGStore.link_entities(kb_id, query)


def _kg_collect(kb_id: int, live: set[int], folder_ids: list[int] | None, top_k: int,
                ent: dict, entities: list[dict]) -> list[ChunkHit]:
    """图谱路同步段（P-02 整体卸载线程池）：图扩展 + 概念同义词 + 倒排回捞 + 证据回捞。"""
    sub = KGStore.expand_on_graph(ent["graph"], [e["id"] for e in entities], depth=1, max_nodes=500)
    edge_chunk_ids = [e.get("evidence_chunk_id") for e in sub["edges"] if e.get("evidence_chunk_id")]
    node_names = [e["name"] for e in entities]
    concept_syns = KGStore.concept_expand(kb_id, [e["concept_id"] for e in entities if e.get("concept_id")])
    names = list({*node_names, *concept_syns})
    if not names and not edge_chunk_ids:
        return []
    # B-08：实体名倒排召回（词元全匹配→分片集合的并集），替代 _any_like 文本扫描
    cid_set: set[int] = set()
    by_key = ent["by_key"]
    for nm in names:
        tks = set(_tokenize(nm))
        if not tks:
            continue
        sets = [by_key.get(t) for t in tks]
        if all(sets):
            inter = set.intersection(*sets)
            cid_set |= inter
    conds = [ChunkMeta.doc_version_id.in_(live or [-1])]
    if folder_ids:
        conds.append(ChunkMeta.folder_id.in_(folder_ids))
    hits: list[ChunkHit] = []
    db = SessionLocal()
    try:
        if cid_set:
            rows = db.scalars(select(ChunkMeta).where(
                ChunkMeta.id.in_(list(cid_set)[:500]), *conds).limit(top_k)).all()
            hits += [ChunkHit(chunk_id=str(r.id), text=r.text, score=0.5, kb_id=r.kb_id,
                              folder_id=r.folder_id, doc_version_id=r.doc_version_id, page=r.page,
                              heading_path=r.heading_path, source="kg",
                              payload={"entities": node_names}) for r in rows]
        if edge_chunk_ids:
            rows2 = db.scalars(select(ChunkMeta).where(
                ChunkMeta.id.in_(edge_chunk_ids[:top_k]),
                ChunkMeta.doc_version_id.in_(live or [-1]))).all()
            hits += [ChunkHit(chunk_id=str(r.id), text=r.text, score=0.6, kb_id=r.kb_id,
                              doc_version_id=r.doc_version_id, page=r.page,
                              heading_path=r.heading_path, source="kg",
                              payload={"via": "edge_evidence"}) for r in rows2]
    finally:
        db.close()
    return hits


async def kg_recall(kb_ids: list[int], query: str, top_k: int = 12,
                    folder_ids: list[int] | None = None,
                    live_map: dict[int, set[int]] | None = None,
                    qvec: list[float] | None = None) -> list[ChunkHit]:
    """图谱关联路（FR-117）：实体链接→多跳扩展→证据 chunk 回捞。多库逐库执行（H6：不再只取首库）。

    B-07：图按库缓存（指纹+TTL 失效），多跳扩展复用缓存图不再每次重建；
    灰度 `runtime_kg_vector_link` 内叠加向量实体链接（别名/近义表达消歧）。
    B-08：实体名回捞走「词元→分片」倒排（主键级 in 查询），替代文本 LIKE 全表扫描。
    P-02：链接/扩展/回捞同步段全部 `asyncio.to_thread` 卸载，事件循环零阻塞。
    live_map（P-06）：入口透传的可检索版本集合，缺省时路内自算（向后兼容）。
    """
    hits: list[ChunkHit] = []
    for kb_id in kb_ids:
        live = (live_map.get(kb_id, set()) if live_map is not None
                else await asyncio.to_thread(_live_versions_one, kb_id))
        # B-07：图缓存（同步建图/倒排卸载线程池，不阻塞事件循环）
        ent = await asyncio.to_thread(_graph_entry, kb_id)
        entities = await asyncio.to_thread(_kg_link, kb_id, query)
        if qvec:
            extra = await _vector_link_safe(kb_id, qvec)
            if not entities:
                entities = extra
            else:
                seen = {e["id"] for e in entities}
                entities += [e for e in extra if e["id"] not in seen]
        if not entities:
            continue
        hits += await asyncio.to_thread(_kg_collect, kb_id, live, folder_ids, top_k, ent, entities)
    return hits


def _graph_entry(kb_id: int):
    from app.engine.kg_graph import graph_cache
    return graph_cache(kb_id)


async def _vector_link_safe(kb_id: int, qvec: list[float]) -> list[dict]:
    """B-07 向量实体链接（灰度内）；任何失败静默降级不影响图谱路。"""
    from app.engine.kg_graph import vector_link, vector_link_enabled
    if not vector_link_enabled():
        return []
    try:
        return await vector_link(kb_id, qvec)
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().exception("向量实体链接失败（降级为字符串链接）| kb=%s", kb_id)
        return []


def _fetch_parents(hits: list[ChunkHit]) -> dict[str, str]:
    """批量回捞 parent 块文本（替代逐候选新开会话的 N+1 写法）。"""
    out: dict[str, str] = {}
    dvs: set[int] = set()
    for h in hits:
        if h.doc_version_id and (h.payload or {}).get("parent_seq", -1) >= 0:
            dvs.add(h.doc_version_id)
    if not dvs:
        return out
    db = SessionLocal()
    try:
        rows = db.scalars(select(ChunkMeta).where(ChunkMeta.doc_version_id.in_(dvs),
                                                  ChunkMeta.role == "parent")).all()
        idx: dict[tuple[int, int], str] = {(r.doc_version_id, r.seq): r.text for r in rows}
        for h in hits:
            pseq = (h.payload or {}).get("parent_seq", -1)
            t = idx.get((h.doc_version_id, pseq))
            if t:
                out[h.chunk_id] = t
    finally:
        db.close()
    return out


def _doc_id_map(version_ids: list[int]) -> dict[int, int]:
    """版本 id → 文档 id 映射（同步段，供 to_thread 卸载）。"""
    if not version_ids:
        return {}
    from app.models import DocVersion as _DV
    db = SessionLocal()
    try:
        return {v.id: v.document_id
                for v in db.scalars(select(_DV).where(_DV.id.in_(version_ids))).all()}
    finally:
        db.close()


async def hybrid_search(kb_ids: list[int], query: str, top_k: int = 5,
                        folder_ids: list[int] | None = None,
                        use_kg: bool = True, use_rerank: bool = True,
                        qvec: list[float] | None = None) -> dict:
    """三路召回 + RRF + 重排 + 富化；返回 {hits, degraded, path_info, routing, attribution}。

    M2 自适应路由（灰度开关 runtime_routing=adaptive 时）：
      standard 档跳过图谱路；deep 档三路全开并按查询特征动态调权（加权 RRF）。
    M1 路级归因：返回每路召回数与最终被采纳（进入候选）的分片来源分布。
    P-01：三路召回 asyncio.gather 并发执行（单路异常只登记降级，不影响其他路）。
    P-03：qvec 可由入口透传复用（同一问题仅嵌入一次），缺省时入口内现算。
    """
    from app.core.logging_config import setup_logging
    log = setup_logging()
    degraded: list[str] = []
    if qvec is None:
        emb = get_embedding()
        try:
            qvec = (await emb.embed([query]))[0]
        except Exception:
            log.exception("Embedding 调用失败，查询向量不可用（降级 embedding）| query=%s", query[:40])
            qvec = None
            degraded.append("embedding")

    # --- M2 路由决策（灰度关闭时行为与现状完全一致）---
    from app.engine.routing import classify, routing_enabled
    adaptive = routing_enabled()
    routing_info: dict = {"tier": "full", "weights": [1.0, 1.0, 1.0], "reasons": [],
                          "adaptive": adaptive, "entities": []}
    kg_enabled = use_kg
    if adaptive and kb_ids:
        try:
            # P-02：规则分类含分词/正则，卸载线程池不阻塞事件循环
            rc = await asyncio.to_thread(classify, kb_ids[0], query)
            routing_info = {"tier": rc["tier"], "weights": rc["weights"], "reasons": rc["reasons"],
                            "adaptive": True, "entities": [e["name"] for e in rc["entities"]][:8]}
            if rc["tier"] == "standard":
                kg_enabled = False  # standard 档跳过图谱路（降本提速，分析文档 §2.1.2）
        except Exception:
            log.exception("路由分类失败，回落全路检索 | query=%s", query[:40])
            degraded.append("routing")

    # P-06：可检索版本集合入口算一次，透传各路（原每路各自重算）
    live_map = await asyncio.to_thread(_live_versions_map, kb_ids) if kb_ids else {}

    # P-01：三路并发（各自独立异常隔离，单路失败降级不影响其余路）
    async def _dense():
        return await dense_recall(kb_ids, qvec, 25, folder_ids) if qvec else []

    async def _bm25():
        return await asyncio.to_thread(bm25_recall, kb_ids, query, 25, folder_ids, live_map)

    async def _kg():
        # B-07：查询向量透传图谱路（向量实体链接消歧，P-03 复用同一次嵌入）
        return await kg_recall(kb_ids, query, 12, folder_ids, live_map,
                               qvec=qvec) if (kg_enabled and kb_ids) else []

    dense_res, bm25_res, kg_res = await asyncio.gather(
        _dense(), _bm25(), _kg(), return_exceptions=True)
    if isinstance(dense_res, BaseException):
        log.exception("稠密召回失败（降级 dense）| kb_ids=%s | query=%s", kb_ids, query[:40],
                      exc_info=dense_res)
        dense_res, degraded = [], degraded + ["dense"]
    if isinstance(bm25_res, BaseException):
        log.exception("BM25 召回失败（降级 bm25）| kb_ids=%s | query=%s", kb_ids, query[:40],
                      exc_info=bm25_res)
        bm25_res, degraded = [], degraded + ["bm25"]
    if isinstance(kg_res, BaseException):
        log.exception("图谱召回失败（降级 kg）| kb_ids=%s | query=%s", kb_ids, query[:40],
                      exc_info=kg_res)
        kg_res, degraded = [], degraded + ["kg"]
    dense_hits, sparse_hits, kg_hits = dense_res, bm25_res, kg_res

    # M1 归因：每路召回数（融合前）
    recall_by_path = {"dense": len(dense_hits), "bm25": len(sparse_hits), "kg": len(kg_hits)}

    lists = [[h.chunk_id for h in dense_hits], [h.chunk_id for h in sparse_hits],
             [h.chunk_id for h in kg_hits]]
    live = [l for l in lists if l]
    if not live:
        # 无命中不是组件降级：degraded 仅记录真实组件故障（embedding/dense/bm25/kg），
        # 空结果由上层走"无依据拒答"分支（BR-004），不再塞入 "empty" 误导前端降级提示
        log.warning("三路召回全部无结果 | kb_ids=%s | q=%s | 降级=%s（若 kb_ids 为空则是检索范围为空）",
                    kb_ids, query[:40], degraded or "无")
        return {"hits": [], "degraded": degraded, "entities": [],
                "routing": routing_info, "attribution": {"recall": recall_by_path, "adopted": {}}}
    if len(live) == 1:
        fused = [(cid, 1.0 - i * 0.01) for i, cid in enumerate(live[0][:top_k * 10])]
    else:
        # M2 加权融合：按路由权重提/降权各路（full 模式权重全 1，等价原逻辑）
        fused = rrf_fuse_weighted(lists, routing_info["weights"], k=60, top=top_k * 10)
    fmap: dict[str, ChunkHit] = {}
    for h in [*dense_hits, *sparse_hits, *kg_hits]:
        prev = fmap.get(h.chunk_id)
        if prev is None:
            fmap[h.chunk_id] = h
        else:
            # 同一分片多路命中：顶层 heading_path/page 用非空者回填，
            # 避免 dense 空顶层遮蔽 BM25/KG 的完整命中（LLM 上下文章节标签丢失会诱发拒答）
            if not prev.heading_path and h.heading_path:
                prev.heading_path = h.heading_path
            if prev.page is None and h.page is not None:
                prev.page = h.page
    candidates = [fmap[cid] for cid, _ in fused if cid in fmap][:50]
    for cand in candidates:
        cand.score = dict(fused).get(cand.chunk_id, cand.score)

    # 运行时配置：sys_config 优先于 .env（P-06：30s TTL 缓存，写路径主动失效）
    from app.core.runtime_config import get_runtime
    _rc = {"runtime_top_k": get_runtime("runtime_top_k"),
           "runtime_relevance_floor": get_runtime("runtime_relevance_floor")}
    # 非法配置值防御：解析失败回落当前值，避免问答整体 500
    try:
        top_k = int(_rc.get("runtime_top_k") or top_k)
    except (TypeError, ValueError):
        log.warning("runtime_top_k 配置非法，回落当前值 %s | raw=%s", top_k, _rc.get("runtime_top_k"))
    from app.config import get_settings as _get_settings
    try:
        floor = float(_rc.get("runtime_relevance_floor") or _get_settings().relevance_floor)
    except (TypeError, ValueError):
        log.warning("runtime_relevance_floor 配置非法，回落默认值 | raw=%s", _rc.get("runtime_relevance_floor"))
        floor = _get_settings().relevance_floor

    rr = get_reranker()
    rerank_used = False
    if use_rerank and candidates:
        try:
            if rr.name != "none":
                candidates = await rr.rerank(query, candidates, top_k)
                rerank_used = True
            else:
                log.warning("重排器未启用（WL2_RERANK_ACTIVE=%s），跳过精排", _get_settings().rerank_active)
                candidates = candidates[:top_k]
        except Exception:
            log.exception("重排执行失败，跳过精排（降级）")
            degraded.append("rerank")
            candidates = candidates[:top_k]
    else:
        candidates = candidates[:top_k]

    # 相关性下限过滤（问题 2：低相关内容不进入回答引用）
    from app.config import get_settings as _gs
    if rerank_used:
        before = len(candidates)
        candidates = [c for c in candidates if c.score >= floor]
        log.info("检索完成 | q=%s | 命中=%d(过滤前 %d) | 重排=%s | top=%.3f | 降级=%s",
                 query[:40], len(candidates), before, rr.name, candidates[0].score if candidates else 0, degraded)
        if not candidates:
            log.info("全部候选低于相关性下限 %.2f，将走拒答/充分性分支", floor)
    else:
        top = candidates[0].score if candidates else 0.0
        candidates = [c for c in candidates if top <= 0 or c.score >= top * 0.3]
        log.info("检索完成(无精排) | q=%s | 命中=%d | top=%.3f | 降级=%s", query[:40], len(candidates), top, degraded)

    # 相邻分片补充已移除：原实现依赖 payload 中的 seq 字段，但 ingest 从未写入该字段，
    # 属从未生效的死代码（审计 H4 确认），移除避免误导。

    # 父子富化：批量回捞 parent 块（P-02：DB 回捞卸载线程池）
    parents = await asyncio.to_thread(_fetch_parents, candidates)
    for cand in candidates:
        cand.payload = {**(cand.payload or {}), "parent_seq": (cand.payload or {}).get("parent_seq", -1),
                        "parent_text": parents.get(cand.chunk_id, cand.text)}
    # 补齐 doc_id（bm25/图谱路的 ChunkHit 无向量库 payload，经版本表映射；P-02 卸载线程池）
    if any(not (c.payload or {}).get("doc_id") for c in candidates):
        dvm = await asyncio.to_thread(_doc_id_map,
                                      [c.doc_version_id for c in candidates
                                       if c.doc_version_id and not (c.payload or {}).get("doc_id")])
        for c in candidates:
            if not (c.payload or {}).get("doc_id"):
                c.payload = {**(c.payload or {}), "doc_id": dvm.get(c.doc_version_id)}
    entities = [e for h in kg_hits for e in (h.payload or {}).get("entities", [])]
    # M1 归因：最终被采纳候选的来源分布（哪路贡献了进入 prompt 的素材）
    adopted: dict[str, int] = {}
    for c in candidates:
        adopted[c.source] = adopted.get(c.source, 0) + 1
    return {"hits": candidates, "degraded": degraded, "entities": list(dict.fromkeys(entities))[:8],
            "rerank": rr.name, "reranked": rerank_used,
            "routing": routing_info,
            "attribution": {"recall": recall_by_path, "adopted": adopted}}
