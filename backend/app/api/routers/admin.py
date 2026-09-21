"""管理面（FR-109/111/112/124/132）：登录、用量看板、清洗规则、审计、健康、配置概况、检索评测。"""
import json
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from app.config import get_settings
from app.core.metering import audit
from app.core.rate_limit import require_login_rate
from app.core.security import create_token, login, require_admin
from app.db import SessionLocal, engine
from app.models import AuditLog, CleanRule, Message, ModelUsage, QAPair
from app.engine.clean import DEFAULT_RULES


class LoginIn(BaseModel):
    username: str
    password: str


login_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _ensure_metering_flushed() -> None:
    """P-10：计量/审计缓冲批写——管理面读取前排空缓冲，保证看板/审计数据可见。"""
    from app.core.metering import flush_now
    flush_now()


@login_router.post("/login", summary="管理员登录（JWT；管理面守门 NFR-219）",
                   dependencies=[Depends(require_login_rate)])
def do_login(body: LoginIn):
    token = login(body.username, body.password)
    if not token:
        audit("admin.login_failed", "user", body.username, detail={"reason": "bad credential"})
        raise HTTPException(401, "账号或密码错误")
    audit("admin.login", "user", body.username, actor=body.username)
    return {"token": token}


router = APIRouter(prefix="/api/admin", tags=["admin"],
                   # S-02：管理面整体守门——审计/用量/评测等读接口与执行接口同样鉴权
                   # （此前仅写操作挂 require_admin，读接口可匿名访问，与 API 描述矛盾）。
                   # /login 在 login_router 上（main.py 单独挂载，不受该依赖约束）。
                   # P-10：计量/审计批量异步刷盘，读取前先排空缓冲，保证看板数据可见。
                   dependencies=[Depends(require_admin), Depends(_ensure_metering_flushed)])


@router.get("/overview", summary="系统总览（今日 Token/降级回答/QA 累计命中）",)
def overview():
    """工作台系统状态条 + 组件健康（NFR-241）。"""
    db = SessionLocal()
    try:
        today = date.today()
        usage = db.execute(select(func.coalesce(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens), 0))
                           .where(ModelUsage.stat_date == today)).scalar()
        tasks_fail = db.scalar(select(func.count()).select_from(Message).where(Message.degraded == True)) or 0  # noqa: E712
        qa_hits = db.scalar(select(func.sum(QAPair.hit_count))) or 0
        return {"today_tokens": int(usage or 0), "degraded_messages": int(tasks_fail),
                "qa_total_hits": int(qa_hits), "daily_limit": get_settings().daily_token_limit}
    finally:
        db.close()


@router.get("/health", summary="组件健康探测（MySQL/Redis/向量库，NFR-241）",)
def health():
    parts = {"api": "up"}
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        parts["db"] = "up"
    except Exception:
        parts["db"] = "down"
    try:
        from app.core.cache import _get_client
        parts["redis"] = "up" if _get_client() else "degraded(memory)"
    except Exception:
        parts["redis"] = "degraded(memory)"
    try:
        from app.providers.vector_store import get_vector_store
        vs = get_vector_store()
        parts["vector"] = vs.name
    except Exception as e:
        parts["vector"] = f"down({e})"
    # LLM / Embedding / Rerank 实际生效实现（轻量实例化，无网络调用；供前端配置面板展示）
    try:
        from app.providers.llm import _resolve_active
        parts["llm"] = _resolve_active(get_settings().llm_active, "llm")
    except Exception as e:
        parts["llm"] = f"down({e})"
    try:
        from app.providers.llm import _resolve_active
        parts["embedding"] = _resolve_active(get_settings().emb_active, "embedding")
    except Exception as e:
        parts["embedding"] = f"down({e})"
    try:
        from app.providers.rerank import get_reranker
        parts["rerank"] = get_reranker().name
    except Exception as e:
        parts["rerank"] = f"down({e})"
    return {"status": "up" if "down" not in parts.values() else "degraded", "components": parts}


@router.get("/usage/summary", summary="用量汇总（今日/本月 Token、节省量；纯用量口径）", description="纯用量口径（2026-09-12 用户裁决）：仅返回 Token 数与节省量，不包含任何价格字段。",)
def usage_summary():
    """纯用量视图（用户裁决 2026-09-12：不展示价格，仅 Token/厂商/模型）。"""
    db = SessionLocal()
    try:
        today = date.today()
        month = today.replace(day=1)
        day_tok = db.execute(select(func.coalesce(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens), 0))
                             .where(ModelUsage.stat_date == today)).scalar() or 0
        month_tok = db.execute(select(func.coalesce(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens), 0))
                               .where(ModelUsage.stat_date >= month)).scalar() or 0
        saved = db.execute(select(func.coalesce(func.sum(Message.tokens_saved), 0))).scalar() or 0
        return {"today_tokens": int(day_tok), "month_tokens": int(month_tok),
                "saved_tokens": int(saved), "daily_limit": get_settings().daily_token_limit}
    finally:
        db.close()


@router.get("/usage/trend", summary="消耗趋势（按日输入/输出 Token）",)
def usage_trend(days: int = 14, kb_id: int | None = None):
    db = SessionLocal()
    try:
        since = date.today() - timedelta(days=days - 1)
        conds = [ModelUsage.stat_date >= since] + ([ModelUsage.kb_id == kb_id] if kb_id else [])
        rows = db.execute(select(ModelUsage.stat_date,
                                 func.sum(ModelUsage.prompt_tokens), func.sum(ModelUsage.completion_tokens))
                          .where(*conds).group_by(ModelUsage.stat_date)).all()
        return [{"date": str(d), "prompt": int(pt or 0), "completion": int(ct or 0)} for d, pt, ct in rows]
    finally:
        db.close()


@router.get("/usage/breakdown", summary="排行与明细（dim=model 含厂商｜kb｜purpose）",)
def usage_breakdown(days: int = 30, dim: str = "model"):
    """Top 排行与明细（FR-132 纯用量口径）：dim=model（含厂商）| kb | purpose。"""
    db = SessionLocal()
    try:
        since = date.today() - timedelta(days=days - 1)
        if dim == "kb":
            rows = db.execute(select(ModelUsage.kb_id, func.max(ModelUsage.provider),
                                     func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens),
                                     func.sum(ModelUsage.call_count))
                              .where(ModelUsage.stat_date >= since).group_by(ModelUsage.kb_id)
                              .order_by(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens).desc())
                              .limit(10)).all()
            return [{"key": str(r[0]), "vendor": r[1] or "", "tokens": int(r[2] or 0),
                     "calls": int(r[3] or 0)} for r in rows]
        if dim == "purpose":
            rows = db.execute(select(ModelUsage.purpose, func.max(ModelUsage.provider),
                                     func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens),
                                     func.sum(ModelUsage.call_count))
                              .where(ModelUsage.stat_date >= since).group_by(ModelUsage.purpose)
                              .order_by(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens).desc())
                              .limit(10)).all()
            return [{"key": str(r[0]), "vendor": r[1] or "", "tokens": int(r[2] or 0),
                     "calls": int(r[3] or 0)} for r in rows]
        rows = db.execute(select(ModelUsage.model_id, func.max(ModelUsage.provider),
                                 func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens),
                                 func.sum(ModelUsage.call_count))
                          .where(ModelUsage.stat_date >= since).group_by(ModelUsage.model_id)
                          .order_by(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens).desc())
                          .limit(10)).all()
        return [{"key": str(r[0]), "vendor": r[1] or "", "tokens": int(r[2] or 0),
                 "calls": int(r[3] or 0)} for r in rows]
    finally:
        db.close()


@router.get("/usage/export", summary="导出用量 CSV（聚合+厂商，无价格列）",)
def usage_export(days: int = 30):
    from fastapi.responses import StreamingResponse
    db = SessionLocal()
    try:
        since = date.today() - timedelta(days=days - 1)
        rows = db.execute(select(ModelUsage).where(ModelUsage.stat_date >= since)).scalars().all()

        def gen():
            yield "date,vendor,model,kb,purpose,prompt_tokens,completion_tokens,calls,errors,estimated\n"
            for u in rows:
                yield f"{u.stat_date},{u.provider},{u.model_id},{u.kb_id or ''},{u.purpose}," \
                      f"{u.prompt_tokens},{u.completion_tokens},{u.call_count},{u.error_count},{u.is_estimated}\n"
    finally:
        db.close()
    audit("admin.usage_export", "usage", "-", detail={"days": days})   # S-09：批量导出补审计
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=usage.csv"})


class CleanRuleIn(BaseModel):
    kb_id: int
    rule_type: str
    pattern: str = ""
    enabled: bool = True
    priority: int = 100


@router.get("/clean-rules", summary="清洗规则列表（按知识库绑定）",)
def list_rules(kb_id: int):
    db = SessionLocal()
    try:
        rows = db.scalars(select(CleanRule).where(CleanRule.kb_id == kb_id)
                          .order_by(CleanRule.priority)).all()
        return [{"id": r.id, "rule_type": r.rule_type, "pattern": r.pattern,
                 "enabled": r.enabled, "priority": r.priority} for r in rows]
    finally:
        db.close()


@router.post("/clean-rules", dependencies=[Depends(require_admin)], summary="新增清洗规则（动作白名单 BR-009）",)
def add_rule(body: CleanRuleIn):
    db = SessionLocal()
    try:
        r = CleanRule(**body.model_dump())
        db.add(r)
        db.commit()
        audit("cleanrule.create", "clean_rule", r.id)
        return {"id": r.id}
    finally:
        db.close()


@router.patch("/clean-rules/{rule_id}", dependencies=[Depends(require_admin)], summary="更新清洗规则（审计留痕）",)
def patch_rule(rule_id: int, body: CleanRuleIn):
    db = SessionLocal()
    try:
        r = db.get(CleanRule, rule_id)
        if not r:
            raise HTTPException(404, "规则不存在")
        for k, v in body.model_dump(exclude={"kb_id"}).items():
            setattr(r, k, v)
        db.commit()
        audit("cleanrule.update", "clean_rule", rule_id)
        return {"ok": True}
    finally:
        db.close()


@router.delete("/clean-rules/{rule_id}", dependencies=[Depends(require_admin)], summary="删除清洗规则",)
def del_rule(rule_id: int):
    db = SessionLocal()
    try:
        r = db.get(CleanRule, rule_id)
        if r:
            db.delete(r)
            db.commit()
        audit("cleanrule.delete", "clean_rule", rule_id)
    finally:
        db.close()
    return {"ok": True}


@router.get("/defaults/clean-rules", summary="内置默认清洗规则集",)
def default_rules():
    return DEFAULT_RULES


@router.get("/audit", summary="审计日志查询（只增，NFR-218）",)
def audit_list(action: str | None = None, page: int = 1, size: int = 50):
    db = SessionLocal()
    try:
        conds = [AuditLog.action.like(f"%{action}%")] if action else []
        total = db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0
        rows = db.scalars(select(AuditLog).where(*conds).order_by(AuditLog.id.desc())
                          .offset((page - 1) * size).limit(size)).all()
        return {"total": total, "items": [{"id": a.id, "actor": a.actor, "action": a.action,
                                           "object_type": a.object_type, "object_id": a.object_id,
                                           "detail": a.detail, "created_at": str(a.created_at or "")}
                                          for a in rows]}
    finally:
        db.close()


@router.post("/eval/run", summary="检索评测执行（批量查询返回命中，FR-124）",)
async def eval_run(kb_ids: list[int], queries: list[str]):
    """检索评测（FR-124 简版）：批量查询返回命中（Recall/MRR 计算在前端评测集对照）。"""
    from app.engine.retrieval import hybrid_search
    out = []
    for q in queries[:100]:
        r = await hybrid_search(kb_ids, q, top_k=10, use_rerank=False)
        out.append({"query": q, "hits": [{"chunk_id": h.chunk_id, "score": round(h.score, 4),
                                          "heading": h.heading_path, "snippet": h.text[:120]}
                                         for h in r["hits"]],
                    "degraded": r["degraded"]})
    return out


# ---------------------------------------------------------------------------
# M1/M6 评估基线：评估集 CRUD + 自动评测 + 历史报表（升级迭代方案 §4.1/§4.6）
# ---------------------------------------------------------------------------
class EvalCaseIn(BaseModel):
    kb_id: int
    case_type: str = "single_hop"   # single_hop|multi_hop|stale|qa
    question: str
    expect: str = ""                # chunk_id 逗号列表 / 关键词 / 空（stale 型）
    enabled: bool = True


@router.get("/eval/cases", summary="评估集列表（M1：单跳/多跳/陈旧探针/QA 四类题型）")
def eval_cases(kb_id: int):
    from app.models import EvalCase
    db = SessionLocal()
    try:
        rows = db.scalars(select(EvalCase).where(EvalCase.kb_id == kb_id).order_by(EvalCase.id)).all()
        return [{"id": c.id, "kb_id": c.kb_id, "case_type": c.case_type, "question": c.question,
                 "expect": c.expect, "enabled": c.enabled} for c in rows]
    finally:
        db.close()


@router.post("/eval/cases", dependencies=[Depends(require_admin)], summary="新增评估题")
def eval_case_add(body: EvalCaseIn):
    from app.models import EvalCase
    if body.case_type not in ("single_hop", "multi_hop", "stale", "qa"):
        raise HTTPException(400, "case_type 须为 single_hop/multi_hop/stale/qa")
    db = SessionLocal()
    try:
        c = EvalCase(kb_id=body.kb_id, case_type=body.case_type, question=body.question,
                     expect=body.expect, enabled=body.enabled)
        db.add(c)
        db.commit()
        audit("eval.case_add", "eval_case", c.id, detail={"kb_id": body.kb_id})
        return {"id": c.id}
    finally:
        db.close()


@router.patch("/eval/cases/{case_id}", dependencies=[Depends(require_admin)], summary="更新评估题（含启停）")
def eval_case_patch(case_id: int, body: EvalCaseIn):
    from app.models import EvalCase
    db = SessionLocal()
    try:
        c = db.get(EvalCase, case_id)
        if not c:
            raise HTTPException(404, "评估题不存在")
        c.case_type, c.question, c.expect, c.enabled = body.case_type, body.question, body.expect, body.enabled
        db.commit()
        audit("eval.case_update", "eval_case", case_id)
        return {"ok": True}
    finally:
        db.close()


@router.delete("/eval/cases/{case_id}", dependencies=[Depends(require_admin)], summary="删除评估题")
def eval_case_del(case_id: int):
    from app.models import EvalCase
    db = SessionLocal()
    try:
        c = db.get(EvalCase, case_id)
        if c:
            db.delete(c)
            db.commit()
        audit("eval.case_delete", "eval_case", case_id)
    finally:
        db.close()
    return {"ok": True}


@router.post("/eval/auto-run", summary="自动评测执行（M1/M6：全题跑批+指标+路级归因落库）")
async def eval_auto_run(kb_id: int):
    """一键评测：逐题走真实检索链路，产出 recall/mrr/stale_pass_rate/qa_accuracy
    与路级归因（各路召回数、Top-K 来源分布、路由档位分布），写入 eval_run。"""
    from app.engine.eval_engine import run_eval
    report = await run_eval(kb_id)
    if report.get("error"):
        raise HTTPException(400, report["error"])
    audit("eval.run", "kb", kb_id, detail={"metrics": report["metrics"]})
    return report


@router.get("/eval/runs", summary="评测历史（指标与归因回看，M1 基线对比）")
def eval_runs(kb_id: int, limit: int = 20):
    from app.models import EvalRun
    db = SessionLocal()
    try:
        rows = db.scalars(select(EvalRun).where(EvalRun.kb_id == kb_id)
                          .order_by(EvalRun.id.desc()).limit(limit)).all()
        return [{"id": r.id, "created_at": str(r.created_at or ""),
                 "metrics": json.loads(r.metrics or "{}"),
                 "attribution": json.loads(r.path_attribution or "{}"),
                 "details": json.loads(r.cases_detail or "[]")} for r in rows]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# M4/M7 一致性对账与陈旧巡检（升级迭代方案 §4.4/§4.7，只读观测）
# ---------------------------------------------------------------------------
@router.post("/recon/run", summary="一致性对账+陈旧探针+向量反向对账（M4/M7/A-08：只读巡检，差异落库）")
async def recon_run(kb_id: int):
    from app.engine.recon import check_consistency, stale_probe, save_recon, vector_recon
    checks = check_consistency(kb_id)
    checks["vector_recon"] = await vector_recon(kb_id)   # A-08：孤儿向量/混维检测
    checks["overall_passed"] = all(c.get("passed", True) for c in checks.values()
                                   if isinstance(c, dict))
    probes = await stale_probe(kb_id)
    rid = save_recon(kb_id, checks, probes)
    stale_failed = [p for p in probes if not p["passed"]]
    if stale_failed or not checks.get("overall_passed", True):
        audit("recon.alert", "kb", kb_id,
              detail={"stale_failed": len(stale_failed), "overall_passed": checks.get("overall_passed"),
                      "orphan_vectors": checks["vector_recon"].get("orphan_count", 0)})
    return {"run_id": rid, "checks": checks, "stale_probe": probes}


@router.get("/recon/runs", summary="巡检历史（对账与探针结果回看）")
def recon_runs(kb_id: int, limit: int = 20):
    from app.models import ReconRun
    db = SessionLocal()
    try:
        rows = db.scalars(select(ReconRun).where(ReconRun.kb_id == kb_id)
                          .order_by(ReconRun.id.desc()).limit(limit)).all()
        return [{"id": r.id, "created_at": str(r.created_at or ""),
                 "checks": json.loads(r.checks or "{}"),
                 "stale_probe": json.loads(r.stale_probe or "[]")} for r in rows]
    finally:
        db.close()


@router.get("/ops/metrics", summary="路级运营看板（M7：召回占比/降级/候选积压/缓存命中）")
def ops_metrics(kb_id: int | None = None):
    """只读运营指标（§4.7）：基于最近评测归因 + 运行态计数。"""
    from app.models import EvalRun, KgCandidate, Message, QAPair
    db = SessionLocal()
    try:
        # 最近一次评测的路级归因（采纳率 = 进入 Top-K 的来源分布）
        conds = [EvalRun.kb_id == kb_id] if kb_id else []
        last_run = db.scalar(select(EvalRun).where(*conds).order_by(EvalRun.id.desc())) if kb_id else None
        attribution = json.loads(last_run.path_attribution or "{}") if last_run else {}
        degraded_msgs = db.scalar(select(func.count()).select_from(Message).where(
            Message.degraded == True)) or 0  # noqa: E712
        qa_total_hits = db.scalar(select(func.sum(QAPair.hit_count))) or 0
        cand_conds = [KgCandidate.status == "pending"] + ([KgCandidate.kb_id == kb_id] if kb_id else [])
        cand_pending = db.scalar(select(func.count()).select_from(KgCandidate).where(*cand_conds)) or 0
        cached = db.execute(select(func.coalesce(func.sum(ModelUsage.cached_tokens), 0))
                            .where(ModelUsage.stat_date >= date.today().replace(day=1))).scalar() or 0
    finally:
        db.close()
    # P-04：语义缓存命中率（用量看板可见）
    from app.engine import semantic_cache
    sem_cache = semantic_cache.metrics()
    return {"attribution": attribution, "degraded_messages": int(degraded_msgs),
            "qa_total_hits": int(qa_total_hits), "candidate_pending": int(cand_pending),
            "month_cached_tokens": int(cached), "semantic_cache": sem_cache}


@router.get("/runtime-config", summary="运行时检索配置（分片/Top-K/重排数，替代 .env 静态值）")
def get_runtime_config():
    from app.models import SysConfig
    from app.engine.chunk import TARGET_TOKENS, OVERLAP_TOKENS
    db = SessionLocal()
    try:
        keys = ["runtime_top_k", "runtime_rerank_top_n", "runtime_relevance_floor"]
        vals = {r.key: r.value for r in db.scalars(select(SysConfig).where(SysConfig.key.in_(keys))).all()}
    finally:
        db.close()
    return {
        "top_k": int(vals.get("runtime_top_k", "5")),
        "rerank_top_n": int(vals.get("runtime_rerank_top_n", "5")),
        "relevance_floor": float(vals.get("runtime_relevance_floor", "0.2")),
    }


@router.put("/runtime-config", dependencies=[Depends(require_admin)])
def put_runtime_config(payload: dict):
    """运行时配置写入 sys_config 表并审计（借鉴 Java 版 settings 表 + 快照审计）。"""
    from app.models import SysConfig
    allowed = {"runtime_top_k": (1, 20), "runtime_rerank_top_n": (1, 10), "runtime_relevance_floor": (0.0, 1.0)}
    db = SessionLocal()
    try:
        for k, v in payload.items():
            if k not in allowed:
                continue
            lo, hi = allowed[k]
            if not (lo <= v <= hi):
                raise HTTPException(400, f"{k} 取值范围 [{lo}, {hi}]")
            row = db.get(SysConfig, k)
            if row:
                row.value = str(v)
                row.version += 1
            else:
                db.add(SysConfig(key=k, value=str(v)))
        db.commit()
        audit("config.update", "sys_config", "runtime", detail=payload)
    finally:
        db.close()
    # P-06：写路径主动失效配置缓存（否则最长 30s 后才生效）
    from app.core.runtime_config import invalidate_runtime
    invalidate_runtime(*[k for k in payload if k in allowed])
    return {"ok": True}
