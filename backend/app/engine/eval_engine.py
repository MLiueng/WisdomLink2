"""评估基线与路级归因引擎（M1/M6，升级迭代方案 §4.1/§4.6）。

三类评估集（§4.1）：
  single_hop —— 单跳事实题（基线守门）
  multi_hop  —— 多跳关系题（M6 图谱路价值测量）
  stale      —— 已删文档探针（期望零召回，验证陈旧内容治理）
  qa         —— QA 字典快路径题

指标口径（对齐分析文档 §7.2，不依赖 LLM 判分的可自动计算部分）：
  recall@10 / mrr@10 / context_precision（首个期望命中的排名倒数近似）
  precision@10 / hit@1 / ndcg@10（检索专项 2026-09-18：数据准确性维度）
  stale_pass_rate（陈旧探针零召回率）/ qa_accuracy（字典命中率）
  路级归因：各路召回数与最终被采纳分片的来源分布（§6.4 原则 10：评估要能归因到路）
"""
import json
import math
from sqlalchemy import select
from app.db import SessionLocal
from app.engine.retrieval import hybrid_search
from app.models import EvalCase, EvalRun

TOP_K = 10
CASE_TYPES = ("single_hop", "multi_hop", "stale", "qa")


def _expect_ids(expect: str) -> list[str] | None:
    """期望为纯 chunk_id 逗号列表时返回 id 列表，否则返回 None（按关键词处理）。"""
    parts = [p.strip() for p in (expect or "").split(",") if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        return parts
    return None


def _ndcg(rels: list[int], k: int) -> float:
    """NDCG@k：二值相关性（0/1），IDCG 为理想排序下前 min(正例数, k) 位的上限。"""
    dcg = sum(r / math.log2(i + 1) for i, r in enumerate(rels[:k], 1) if r)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(sum(rels), k) + 1))
    return round(dcg / ideal, 4) if ideal else 0.0


async def _eval_one(case: EvalCase, kb_id: int) -> dict:
    """单题评估；返回 {case_id, type, question, passed, recall, rank, tier, sources}。"""
    detail = {"case_id": case.id, "type": case.case_type, "question": case.question,
              "passed": False, "recall": 0.0, "rank": None, "tier": "", "sources": [],
              "precision": 0.0, "hit1": 0, "ndcg": 0.0}

    if case.case_type == "qa":
        from app.engine import qa_engine
        try:
            hit = await qa_engine.try_hit([kb_id], case.question)
        except Exception:
            hit = None
        detail["passed"] = bool(hit)
        return detail

    try:
        r = await hybrid_search([kb_id], case.question, top_k=TOP_K, use_rerank=True)
    except Exception:
        r = {"hits": [], "routing": {}, "attribution": {"recall": {}, "adopted": {}}}
    hits = r["hits"]
    detail["tier"] = (r.get("routing") or {}).get("tier", "")
    detail["sources"] = [h.source for h in hits]

    if case.case_type == "stale":
        # 探针题：召回分片不得来自已删除文档（泛化查询召回其他在架内容属正常语义）
        from app.engine.recon import hits_from_deleted
        detail["passed"] = hits_from_deleted(hits) == 0
        return detail

    if not hits:
        return detail

    exp_ids = _expect_ids(case.expect)
    if exp_ids is not None:
        hit_ids = [h.chunk_id for h in hits]
        found = [e for e in exp_ids if e in hit_ids]
        detail["recall"] = round(len(found) / len(exp_ids), 4) if exp_ids else 0.0
        detail["passed"] = len(found) == len(exp_ids)
        first = next((i for i, cid in enumerate(hit_ids, 1) if cid in exp_ids), None)
        detail["rank"] = first
        rels = [1 if cid in exp_ids else 0 for cid in hit_ids]
    else:
        keywords = [k.strip() for k in (case.expect or "").replace("/", ",").split(",") if k.strip()]
        rank = None
        for i, h in enumerate(hits, 1):
            if any(k and k in h.text for k in keywords):
                rank = i
                break
        detail["rank"] = rank
        detail["recall"] = 1.0 if rank else 0.0
        detail["passed"] = rank is not None
        rels = [1 if any(k and k in h.text for k in keywords) else 0 for h in hits]
    # 数据准确性维度（检索专项 2026-09-18）：命中占比 / 首位命中 / 排序质量
    detail["precision"] = round(sum(rels) / len(rels), 4) if rels else 0.0
    detail["hit1"] = 1 if rels and rels[0] == 1 else 0
    detail["ndcg"] = _ndcg(rels, TOP_K)
    return detail


async def run_eval(kb_id: int) -> dict:
    """执行该库全部启用评估题，汇总指标并落库（EvalRun），返回完整报告。"""
    db = SessionLocal()
    try:
        cases = db.scalars(select(EvalCase).where(EvalCase.kb_id == kb_id,
                                                  EvalCase.enabled == True)).all()  # noqa: E712
    finally:
        db.close()
    if not cases:
        return {"error": "评估集为空，请先添加评估题（POST /api/admin/eval/cases）", "total": 0}

    details: list[dict] = []
    tier_dist: dict[str, int] = {}
    for c in cases:
        d = await _eval_one(c, kb_id)
        details.append(d)
        tier_dist[d["tier"] or "full"] = tier_dist.get(d["tier"] or "full", 0) + 1

    def _by_type(t: str) -> list[dict]:
        return [d for d in details if d["type"] == t]

    def _avg(vals: list[float]) -> float:
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    ret_cases = [d for d in details if d["type"] in ("single_hop", "multi_hop")]
    mrr_vals = [1.0 / d["rank"] for d in ret_cases if d.get("rank")]
    stale = _by_type("stale")
    qa = _by_type("qa")
    metrics = {
        "total": len(details),
        "pass_rate": _avg([1.0 if d["passed"] else 0.0 for d in details]),
        "recall": _avg([d["recall"] for d in ret_cases]),
        "mrr": _avg(mrr_vals),
        # context_precision 近似：首个期望命中排名倒数（RAGAS CP 的轻量替代口径）
        "context_precision": _avg([1.0 / d["rank"] for d in ret_cases if d.get("rank")]),
        # 数据准确性（检索专项 2026-09-18）：命中占比 / 首位命中率 / 排序质量
        "precision": _avg([d.get("precision", 0.0) for d in ret_cases]),
        "hit1": _avg([float(d.get("hit1", 0)) for d in ret_cases]),
        "ndcg": _avg([d.get("ndcg", 0.0) for d in ret_cases]),
        "stale_pass_rate": _avg([1.0 if d["passed"] else 0.0 for d in stale]) if stale else None,
        "qa_accuracy": _avg([1.0 if d["passed"] else 0.0 for d in qa]) if qa else None,
        "by_type": {t: {"count": len(_by_type(t)),
                        "pass_rate": _avg([1.0 if d["passed"] else 0.0 for d in _by_type(t)])}
                    for t in CASE_TYPES},
        "multi_hop_recall": _avg([d["recall"] for d in _by_type("multi_hop")]),  # M6 决策依据
    }

    # 路级归因汇总：被采纳（进入 Top-K）分片的来源分布 + 路由档位分布
    adopted: dict[str, int] = {}
    for d in details:
        for s in d.get("sources", []):
            adopted[s] = adopted.get(s, 0) + 1
    attribution = {"adopted_topk": adopted, "routing_tiers": tier_dist}

    db = SessionLocal()
    try:
        db.add(EvalRun(kb_id=kb_id, metrics=json.dumps(metrics, ensure_ascii=False),
                       path_attribution=json.dumps(attribution, ensure_ascii=False),
                       cases_detail=json.dumps(details, ensure_ascii=False)))
        db.commit()
    finally:
        db.close()
    return {"metrics": metrics, "attribution": attribution, "details": details}
