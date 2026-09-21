"""B-2 向量实体链接阈值实测扫参（《升级迭代改进建议方案》§8.13 SOP，白天远程窗口）。

前置：真实 .env（远程智谱嵌入 + Milvus 172.21.102.1 + 远程 rerank api）。
流程：
  1. 多跳评估题补齐（现仅 1 题，样本不足以判定阈值；幂等插入）；
  2. 开启灰度 `sys_config.runtime_kg_vector_link=on`；
  3. 扫参 0.75~0.95（步长 0.05），每档记录：
     a. vector_link 直测：每题链接实体与分数 → 误链判定（链接到标注集外实体）；
     b. 端到端 _eval_one：multi_hop 题 recall / MRR / kg 路采纳数；
  4. 判定：recall 峰值且误链率 ≤5%（6+1 题口径 → 要求零误链题）；并列取高阈值；
  5. 产出 `data/b2_sweep_report.json` 与控制台对比表；阈值复原、灰度恢复 off。

运行：.venv/Scripts/python tests/sweep_kg_link_threshold.py
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KB = 1
# 复扫区间（2026-09-20 实测：问句×实体相似度峰值区 0.70~0.80，原 0.75~0.95 落在分布右尾外）
THRESHOLDS = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
# 链接覆盖率下限：低于该比例时阈值判定无意义（实体积累不足，应先补实体/别名再复扫）
MIN_LINK_COVERAGE = 0.5

# 多跳评估题（问题 / expect 关键词 / 预期相关实体集合——误链判定基准，含常见变体写法）
MH_CASES = [
    ("员工报销由财务总监审批吗", "财务总监",
     {"员工", "报销", "财务总监", "财务", "差旅报销", "差旅报销制度", "差旅报销制度（2026修订版）",
      "差旅报销制度（2026 修订版）", "差旅费用管理", "OA系统", "OA 系统"}),
    ("请假超过3天需要哪一级审批", "人力资源总监",
     {"请假流程", "人力资源总监", "部门总监", "直属主管", "考勤与假期管理制度"}),
    ("远程办公用的EasyConnect用什么账号登录", "域账号",
     {"VPN", "EasyConnect", "域账号", "内网门户", "办公网络"}),
    ("执行回滚操作依赖哪个平台", "K8s",
     {"回滚", "回滚方案", "K8s", "发布步骤", "产品发布流程SOP", "产品发布流程 SOP", "产品发布"}),
    ("打印机卡单先查看哪个服务器的队列", "打印服务器",
     {"打印机故障", "打印服务器", "IT服务台", "IT 服务台"}),
    ("紧急修复发布需要谁口头批准", "技术负责人",
     {"紧急修复", "发布窗口", "技术负责人", "产品发布流程SOP", "产品发布流程 SOP"}),
    ("会议室投影仪使用前要到哪里登记", "IT服务台",
     {"投影仪", "会议室", "会议室预订", "会议室使用规范", "IT服务台", "IT 服务台"}),
]


def _ensure_schema() -> None:
    """脚本直跑不经 app lifespan，补齐新表（create_all 幂等，不动既有表）。"""
    from app.db import Base, engine
    import app.models  # noqa: F401  注册全部模型
    Base.metadata.create_all(engine)


def _check_edge_column() -> None:
    """前置自检：kg_edge.evidence_clean_hash 列必须存在（教训：2026-09-20 首轮扫参时
    缺列导致图谱路全程静默降级，kg_adopted=0 为假象）。缺列时中止并提示走启动迁移。"""
    import sqlite3
    from app.config import get_settings
    url = get_settings().db_url
    if not url.startswith("sqlite"):
        return
    path = url.split("sqlite:///", 1)[-1]
    con = sqlite3.connect(path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(kg_edge)")]
    finally:
        con.close()
    if "evidence_clean_hash" not in cols:
        raise SystemExit(
            "前置自检失败：kg_edge 缺 evidence_clean_hash 列，图谱路将静默降级、"
            "扫参结论无效。请先启动一次后端（app/main.py 启动迁移自动补列）再复扫。")


def _ensure_cases() -> None:
    """幂等补齐多跳评估题（按问题文本判重）。"""
    from app.db import SessionLocal
    from app.models import EvalCase
    db = SessionLocal()
    try:
        existing = {c.question for c in db.query(EvalCase).filter(EvalCase.kb_id == KB)}
        added = 0
        for q, exp, _ents in MH_CASES:
            if q not in existing:
                db.add(EvalCase(kb_id=KB, case_type="multi_hop", question=q, expect=exp))
                added += 1
        db.commit()
        print(f"评估题补齐：新增 {added} 题（既有跳过 {len(MH_CASES) - added}）")
    finally:
        db.close()


def _set_vector_link(on: bool) -> None:
    """灰度开关写 sys_config（含 version/updated_at NOT NULL 约束）+ 失效运行时缓存。"""
    from app.db import SessionLocal
    from app.models import SysConfig
    from app.core.runtime_config import invalidate_runtime
    db = SessionLocal()
    try:
        row = db.get(SysConfig, "runtime_kg_vector_link")
        if row:
            row.value = "on" if on else "off"
            row.version = (row.version or 1) + 1
            row.updated_at = datetime.now()
        else:
            db.add(SysConfig(key="runtime_kg_vector_link", value="on" if on else "off",
                             version=1, updated_at=datetime.now()))
        db.commit()
    finally:
        db.close()
    invalidate_runtime("runtime_kg_vector_link")


EXPECTED: dict[str, set[str]] = {q: ents for q, _e, ents in MH_CASES}


async def main() -> None:
    from sqlalchemy import select
    from app.config import get_settings
    from app.db import SessionLocal
    from app.engine.eval_engine import _eval_one
    from app.engine.kg_graph import vector_link
    from app.models import EvalCase
    from app.providers.embedding import get_embedding

    _ensure_schema()
    _check_edge_column()
    _ensure_cases()
    _set_vector_link(True)

    db = SessionLocal()
    try:
        cases = db.scalars(select(EvalCase).where(
            EvalCase.kb_id == KB, EvalCase.case_type == "multi_hop",
            EvalCase.enabled == True)).all()  # noqa: E712
    finally:
        db.close()
    print(f"参评多跳题：{len(cases)} 道")

    emb = get_embedding()
    qvecs: dict[int, list[float]] = {}
    for c in cases:
        qvecs[c.id] = (await emb.embed([c.question]))[0]

    settings = get_settings()
    original_thr = settings.kg_link_sim_threshold
    # --- 相似度直测（不受阈值过滤）：定位实体向量与问句的实际相似度分布，
    #     作为扫参区间有效性的判据（首扫教训：区间落在分布右尾外时结论失真）---
    from app.engine.kg_graph import _node_vectors
    from app.providers.embedding import cosine
    from app.models import KGNode
    vecs = await _node_vectors(KB)
    db = SessionLocal()
    try:
        node_names = {n.id: n.name for n in db.scalars(select(KGNode).where(
            KGNode.kb_id == KB, KGNode.status == "confirmed"))}
    finally:
        db.close()
    sim_profile: list[dict] = []
    if vecs:
        for c in cases:
            scored = sorted(((nid, cosine(qvecs[c.id], v)) for nid, v in vecs.items() if v),
                            key=lambda x: x[1], reverse=True)
            top = [(node_names.get(nid, str(nid)), round(s, 4)) for nid, s in scored[:3]]
            sim_profile.append({"q": c.question, "top3": top,
                                "max": round(scored[0][1], 4) if scored else 0.0})
    linked_any = 0   # 最低阈值档下产生过链接的题数（覆盖率判据）
    report: list[dict] = []
    try:
        for thr in THRESHOLDS:
            settings.kg_link_sim_threshold = thr
            row: dict = {"threshold": thr, "cases": [], "false_link_questions": 0,
                         "kg_adopted": 0, "linked_questions": 0}
            recalls, mrrs = [], []
            for c in cases:
                t0 = time.perf_counter()
                links = await vector_link(KB, qvecs[c.id], threshold=thr)
                names = [h["name"] for h in links]
                exp_set = EXPECTED.get(c.question)
                false = [n for n in names if exp_set is not None and n not in exp_set]
                if names:
                    row["linked_questions"] += 1
                    if thr == THRESHOLDS[0]:
                        linked_any += 1
                if false:
                    row["false_link_questions"] += 1
                d = await _eval_one(c, KB)
                if d.get("recall") is not None:
                    recalls.append(d["recall"])
                if d.get("rank"):
                    mrrs.append(1.0 / d["rank"])
                row["kg_adopted"] += sum(1 for s in d.get("sources", []) if s == "kg")
                row["cases"].append({
                    "q": c.question, "links": names, "false": false,
                    "recall": d.get("recall"), "rank": d.get("rank"),
                    "kg_hits": sum(1 for s in d.get("sources", []) if s == "kg"),
                    "latency_ms": int((time.perf_counter() - t0) * 1000)})
            row["recall"] = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
            row["mrr"] = round(sum(mrrs) / len(mrrs), 4) if mrrs else 0.0
            row["false_link_rate"] = round(row["false_link_questions"] / len(cases), 4)
            report.append(row)
            print(f"thr={thr:.2f} | recall={row['recall']:.4f} mrr={row['mrr']:.4f} "
                  f"kg采纳={row['kg_adopted']} 链接题数={row['linked_questions']}/{len(cases)} "
                  f"误链题={row['false_link_questions']}/{len(cases)}")
    finally:
        settings.kg_link_sim_threshold = original_thr
        _set_vector_link(False)   # 灰度纪律：扫参完毕恢复 off，开启需另行确认

    # --- 前置判据：链接覆盖率（最低档下产生链接的题占比）---
    link_coverage = linked_any / len(cases) if cases else 0.0
    sufficient = link_coverage >= MIN_LINK_COVERAGE

    # --- 判定：recall 峰值且误链率 ≤5%；并列取高阈值（宁缺毋滥）---
    max_recall = max(r["recall"] for r in report)
    candidates = [r for r in report if r["recall"] >= max_recall - 1e-9
                  and r["false_link_rate"] <= 0.05]
    chosen = max(candidates, key=lambda r: r["threshold"]) if candidates else None

    out = {"at": datetime.now().isoformat(timespec="seconds"),
           "kb_id": KB, "case_count": len(cases), "thresholds": report,
           "sim_profile": sim_profile,
           "link_coverage": round(link_coverage, 4),
           "coverage_sufficient": sufficient,
           "chosen": chosen["threshold"] if chosen else None,
           "rule": "recall 峰值且误链率≤5%；并列取高阈值；覆盖率<50% 时仅记录不固化"}
    Path("./data/b2_sweep_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n===== 相似度直测（top3 实体）=====")
    for p in sim_profile:
        print(f"  「{p['q']}」max={p['max']:.3f} | {p['top3']}")
    print("\n===== B-2 扫参结果 =====")
    print(f"链接覆盖率（最低档 {THRESHOLDS[0]}）：{linked_any}/{len(cases)} "
          f"= {link_coverage:.0%}{'（充分，可判定）' if sufficient else '（不足，本轮仅记录不固化）'}")
    print(f"{'thr':>6} {'recall':>8} {'mrr':>8} {'kg采纳':>6} {'链接题':>6} {'误链题':>7}")
    for r in report:
        mark = " <== 推荐" if chosen and r["threshold"] == chosen["threshold"] else ""
        print(f"{r['threshold']:>6.2f} {r['recall']:>8.4f} {r['mrr']:>8.4f} "
              f"{r['kg_adopted']:>6} {r['linked_questions']:>4}/{len(cases)}"
              f" {r['false_link_questions']:>5}/{len(cases)}{mark}")
    if not sufficient:
        print("\n结论：实体向量覆盖不足（问句×实体相似度峰值区落在扫参区间外），"
              "阈值判定暂缓；请先补实体/别名（抽取确认入库）后复扫。")
    elif chosen:
        print(f"\n推荐阈值：{chosen['threshold']}（recall={chosen['recall']}，"
              f"误链率={chosen['false_link_rate']}）")
    else:
        print("\n无满足误链率≤5% 的档位，维持默认 0.85 并需人工复核误链明细")
    print("报告已归档：backend/data/b2_sweep_report.json")


if __name__ == "__main__":
    asyncio.run(main())
