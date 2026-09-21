"""M1–M7 全流程测试（真实后端 + 真实模型链路，灰度开关原位恢复）。

前置：后端已启动于 127.0.0.1:8000（默认开发库 data/wl2.db，kb_id=1）。
运行：python tests/fullflow_m1_m7.py
覆盖：
  M1/M6 评估集 CRUD + 一键评测 + 指标/路级归因 + 历史 + 多跳单独测量
  M2    灰度开关 full→adaptive 路由档位切换对比（测后恢复）
  M3    Wiki 发布 → 自动抽取入队 → 详情/确认/拒绝/指纹去重
  M4/M7 一致性对账 + 陈旧探针 + 巡检历史 + 运营指标
  M5    cached_tokens 落库与月度汇总
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
DB = str(ROOT / "data" / "wl2.db")
KB = 1

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def req(method, path, body=None, token=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except json.JSONDecodeError:
            return e.code, txt


def upload_md(path, token, fields, fname, content):
    boundary = "----wl2fullflowboundary"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{fname}\"\r\nContent-Type: text/markdown\r\n\r\n").encode()
    body += content.encode() + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + path, data=body, method="POST",
                               headers={"Authorization": "Bearer " + token,
                                        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


def db_set(key, value):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO sys_config(key,value,version,updated_at) VALUES(?,?,1,CURRENT_TIMESTAMP) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    c.commit()
    c.close()


def db_get(key):
    c = sqlite3.connect(DB)
    r = c.execute("select value from sys_config where key=?", (key,)).fetchone()
    c.close()
    return r[0] if r else None


def main():
    # 清理本脚本历史残留（评估集/巡检报告/候选/测试 wiki 文档），保证可重复执行
    c = sqlite3.connect(DB)
    c.execute(f"delete from eval_case where kb_id={KB}")
    c.execute(f"delete from eval_run where kb_id={KB}")
    c.execute(f"delete from kg_candidate where kb_id={KB}")
    del_doc_ids = []
    for t in ("费用审批系统测试页A", "会议室预订规则测试页B"):
        ids = [r[0] for r in c.execute(
            f"select id from document where kb_id={KB} and title=?", (t,))]
        for did in ids:
            del_doc_ids.append(did)
            c.execute("delete from chunk_meta where kb_id=? and doc_version_id in "
                      "(select id from doc_version where document_id=?)", (KB, did))
            c.execute("delete from doc_version where document_id=?", (did,))
            c.execute("delete from document where id=?", (did,))
    c.commit()
    c.close()
    # 直删库行不经删除 API，向量需自行补偿清理（否则残留孤儿向量污染召回）
    if del_doc_ids:
        import asyncio
        from app.providers.vector_store import get_vector_store
        from app.engine.retrieval import invalidate_bm25

        async def _purge():
            vs = get_vector_store()
            for did in del_doc_ids:
                await vs.delete_by(f"chunk_{KB}", {"in": {"doc_id": [did]}})
        asyncio.run(_purge())
        invalidate_bm25(KB)
        print(f"[清理] 历史残留已清除（文档 {del_doc_ids}，含向量补偿）")

    # 0. 登录
    st, tok = req("POST", "/api/admin/login", {"username": "admin", "password": "admin123"})
    assert st == 200 and tok.get("token"), f"登录失败: {st} {tok}"
    h = tok["token"]
    print("[0] 登录成功")

    # ============ M1/M6：评估集 CRUD + 一键评测 + 归因 + 历史 ============
    print("\n===== M1/M6 评估基线与路级归因 =====")
    cases = [
        {"kb_id": KB, "case_type": "single_hop", "question": "一线城市住宿报销标准是多少", "expect": "600元"},
        {"kb_id": KB, "case_type": "multi_hop", "question": "员工报销由财务总监审批吗", "expect": "财务总监"},
        {"kb_id": KB, "case_type": "stale", "question": "回归测试页", "expect": ""},
        {"kb_id": KB, "case_type": "qa", "question": "公司WiFi密码是多少", "expect": ""},
    ]
    case_ids = []
    for c in cases:
        st, r = req("POST", "/api/admin/eval/cases", c, h)
        check(f"M1 新增评估题[{c['case_type']}]", st == 200 and r.get("id"), f"id={r.get('id')}")
        case_ids.append(r.get("id"))

    st, lst = req("GET", f"/api/admin/eval/cases?kb_id={KB}", token=h)
    check("M1 评估集列表", st == 200 and len(lst) >= 4, f"共 {len(lst)} 题")

    st, report = req("POST", f"/api/admin/eval/auto-run?kb_id={KB}", token=h, timeout=600)
    m = report.get("metrics", {}) if isinstance(report, dict) else {}
    check("M1 一键评测执行", st == 200 and m.get("total") == len(cases),
          f"total={m.get('total')} pass_rate={m.get('pass_rate')}")
    check("M1 recall/mrr 指标", m.get("recall") is not None and m.get("mrr") is not None,
          f"recall@10={m.get('recall')} mrr@10={m.get('mrr')} cp={m.get('context_precision')}")
    check("M1 陈旧探针通过率", m.get("stale_pass_rate") == 1.0,
          f"stale_pass_rate={m.get('stale_pass_rate')}（已删文档零召回）")
    check("M1 QA 命中率", m.get("qa_accuracy") == 1.0, f"qa_accuracy={m.get('qa_accuracy')}")
    check("M6 多跳召回单独测量", "multi_hop_recall" in m, f"multi_hop_recall={m.get('multi_hop_recall')}")
    attr = report.get("attribution", {})
    check("M1 路级归因（采纳来源分布）", bool(attr.get("adopted_topk")),
          f"adopted_topk={attr.get('adopted_topk')}")
    check("M1 路由档位分布（full 基线）", attr.get("routing_tiers", {}).get("full", 0) >= 3,
          f"routing_tiers={attr.get('routing_tiers')}")
    detail_tiers = {d["type"]: d.get("tier") for d in report.get("details", [])}
    check("M1 逐题明细含档位/来源", all(d.get("sources") is not None for d in report.get("details", [])),
          f"tiers={detail_tiers}")

    st, runs = req("GET", f"/api/admin/eval/runs?kb_id={KB}", token=h)
    check("M1 评测历史落库", st == 200 and len(runs) >= 1 and runs[0].get("metrics", {}).get("total"),
          f"历史 {len(runs)} 条")

    # PATCH/DELETE（用临时题验证，不影响基线集）
    st, r = req("POST", "/api/admin/eval/cases",
                {"kb_id": KB, "case_type": "single_hop", "question": "临时题", "expect": "x"}, h)
    tmp_id = r.get("id")
    st2, _ = req("PATCH", f"/api/admin/eval/cases/{tmp_id}",
                 {"kb_id": KB, "case_type": "single_hop", "question": "临时题改", "expect": "y",
                  "enabled": False}, h)
    st3, _ = req("DELETE", f"/api/admin/eval/cases/{tmp_id}", token=h)
    check("M1 评估题 PATCH/DELETE", st == 200 and st2 == 200 and st3 == 200, "增改删全通")

    # ============ M2：自适应路由灰度对比 ============
    print("\n===== M2 自适应路由（灰度开关对比） =====")
    origin_routing = db_get("runtime_routing")
    db_set("runtime_routing", "adaptive")
    # P-06：runtime_config 进程内 30s TTL 缓存（叠加 P-02 stale-while-revalidate），
    # 直写 SQLite 不触发失效；等待 TTL 过期后，评测首题触发后台续期、后续题即读新值
    time.sleep(32)
    st, report2 = req("POST", f"/api/admin/eval/auto-run?kb_id={KB}", token=h, timeout=600)
    tiers2 = report2.get("attribution", {}).get("routing_tiers", {}) if isinstance(report2, dict) else {}
    routed = {d["question"]: (d.get("tier"), d.get("sources")) for d in report2.get("details", [])
              if d["type"] in ("single_hop", "multi_hop", "stale")}
    adaptive_ok = st == 200 and any(t in ("standard", "deep") for t, _ in routed.values())
    check("M2 adaptive 档路由生效", adaptive_ok,
          f"routing_tiers={tiers2} | " + "; ".join(f"{q[:10]}→{t}" for q, (t, _) in routed.items()))
    deep_q = [q for q, (t, _) in routed.items() if t == "deep"]
    std_q = [q for q, (t, _) in routed.items() if t == "standard"]
    check("M2 分层结论（标准/深度）", bool(deep_q or std_q),
          f"deep={deep_q} standard={std_q}")
    # 恢复开关
    if origin_routing is None:
        c = sqlite3.connect(DB)
        c.execute("delete from sys_config where key='runtime_routing'")
        c.commit()
        c.close()
    else:
        db_set("runtime_routing", origin_routing)
    check("M2 灰度开关已恢复", db_get("runtime_routing") == origin_routing,
          f"runtime_routing={db_get('runtime_routing') or '(缺省 full)'}")

    # ============ M3：变更驱动抽取（发布钩子 → 候选 → 确认/拒绝/去重） ============
    print("\n===== M3 变更驱动抽取 =====")
    wiki1 = ("# 费用审批系统（全流程测试页A）\n\n"
             "费用审批系统由报销提交模块与审批流模块组成。报销提交模块依赖OA系统。\n"
             "员工通过OA系统提交报销单，财务总监负责最终审批。\n"
             "财务部门在每月15日与30日统一打款。\n")
    r = upload_md("/api/documents/upload", h, {"kb_id": str(KB), "source_type": "wiki"},
                  "费用审批系统测试页A.md", wiki1)
    doc_a = r["id"]
    print(f"  wiki 文档A 上传成功 doc_id={doc_a}，等待入库发布…")
    status = ""
    for _ in range(120):
        st, d = req("GET", f"/api/documents/{doc_a}", token=h)
        status = d.get("status", "")
        if status in ("published", "failed"):
            break
        time.sleep(1)
    check("M3 wiki 文档入库发布", status == "published", f"status={status}")

    cand_a = None
    for _ in range(90):
        st, lst = req("GET", f"/api/kg/{KB}/extract-candidates?status=pending", token=h)
        cand_a = next((i for i in lst.get("items", []) if i["doc_id"] == doc_a), None)
        if cand_a:
            break
        time.sleep(2)
    check("M3 发布自动抽取入队", bool(cand_a),
          f"candidate={cand_a['id'] if cand_a else '未入队'} 实体{cand_a and cand_a['entities']} "
          f"关系{cand_a and cand_a['relations']}")

    if cand_a:
        st, det = req("GET", f"/api/kg/extract-candidates/{cand_a['id']}", token=h)
        p = det.get("payload", {})
        check("M3 候选详情", st == 200 and (p.get("entities") or p.get("relations")),
              f"实体{len(p.get('entities', []))} 关系{len(p.get('relations', []))} chunk_ids={len(p.get('chunk_ids', []))}")

        # 运营指标：确认前应计入候选积压（此时至少 cand_a 为 pending）
        st, ops = req("GET", "/api/admin/ops/metrics", token=h)
        check("M7 候选积压计入运营指标", ops.get("candidate_pending", 0) >= 1,
              f"candidate_pending={ops.get('candidate_pending')}")

        st, cr = req("POST", f"/api/kg/extract-candidates/{cand_a['id']}/confirm", token=h)
        check("M3 候选确认入图", st == 200 and cr.get("ok"),
              f"写入图谱候选态：实体{cr.get('entities')} 关系{cr.get('relations')}")
        st, lst = req("GET", f"/api/kg/{KB}/extract-candidates?status=confirmed", token=h)
        check("M3 确认后状态流转", any(i["id"] == cand_a["id"] for i in lst.get("items", [])),
              "confirmed 列表可见")

        # 指纹去重：同版本重复触发应跳过
        st, dup = req("POST", f"/api/kg/{KB}/extract-candidates/{doc_a}/trigger", token=h)
        check("M3 指纹去重（重复触发跳过）", dup.get("skipped") is True,
              f"reason={dup.get('reason')}")

    # 拒绝分支：第二篇 wiki 页
    wiki2 = ("# 会议室预订规则（全流程测试页B）\n\n"
             "会议室预订系统属于行政部管理。员工通过内网OA预订会议室，行政专员负责审批。\n")
    r = upload_md("/api/documents/upload", h, {"kb_id": str(KB), "source_type": "wiki"},
                  "会议室预订规则测试页B.md", wiki2)
    doc_b = r["id"]
    for _ in range(120):
        st, d = req("GET", f"/api/documents/{doc_b}", token=h)
        if d.get("status") in ("published", "failed"):
            break
        time.sleep(1)
    cand_b = None
    for _ in range(90):
        st, lst = req("GET", f"/api/kg/{KB}/extract-candidates?status=pending", token=h)
        cand_b = next((i for i in lst.get("items", []) if i["doc_id"] == doc_b), None)
        if cand_b:
            break
        time.sleep(2)
    if cand_b:
        st, rr = req("POST", f"/api/kg/extract-candidates/{cand_b['id']}/reject", token=h)
        check("M3 候选拒绝", st == 200 and rr.get("ok"), "不入图，留档审计")
        # 已处理候选不可重复操作
        st2, rr2 = req("POST", f"/api/kg/extract-candidates/{cand_b['id']}/confirm", token=h)
        check("M3 重复操作拦截", st2 == 400, f"HTTP {st2}: {rr2.get('detail', '')}")
    else:
        check("M3 候选拒绝", False, "文档B 未产生候选")

    # ============ M4/M7：对账巡检 + 运营指标 ============
    print("\n===== M4/M7 对账巡检与运营指标 =====")
    st, rc = req("POST", f"/api/admin/recon/run?kb_id={KB}", token=h, timeout=600)
    checks = rc.get("checks", {}) if isinstance(rc, dict) else {}
    probes = rc.get("stale_probe", [])
    check("M4 一致性对账执行", st == 200 and "docs_vs_chunks" in checks,
          f"docs={checks.get('docs_vs_chunks', {}).get('doc_count')} "
          f"chunks={checks.get('docs_vs_chunks', {}).get('chunk_count')} "
          f"overall={checks.get('overall_passed')}")
    check("M4 图谱证据有效率", "kg_evidence_validity" in checks,
          f"rate={checks.get('kg_evidence_validity', {}).get('rate')}")
    check("M7 陈旧探针执行", len(probes) >= 2, f"{len(probes)} 个探针（已删/下线文档）")
    probe_fail = [p for p in probes if not p["passed"]]
    check("M7 已删文档零召回", not probe_fail,
          f"失败探针={[(p['title'], p['recall_count']) for p in probe_fail] or '无'}")

    st, runs = req("GET", f"/api/admin/recon/runs?kb_id={KB}", token=h)
    check("M4 巡检历史落库", st == 200 and len(runs) >= 1, f"历史 {len(runs)} 条")

    st, ops = req("GET", "/api/admin/ops/metrics", token=h)
    check("M7 运营指标接口", st == 200 and all(k in ops for k in
          ("attribution", "degraded_messages", "qa_total_hits", "candidate_pending", "month_cached_tokens")),
          f"qa_total_hits={ops.get('qa_total_hits')} degraded={ops.get('degraded_messages')} "
          f"candidate_pending={ops.get('candidate_pending')}")

    # ============ M5：cached_tokens 落库与汇总 ============
    print("\n===== M5 Prompt Caching 计量 =====")
    from app.core.metering import record_usage, flush_now
    before = req("GET", "/api/admin/ops/metrics", token=h)[1].get("month_cached_tokens", 0)
    record_usage("m5-test-model", "TestVendor", "qa", KB, 1000, 200, False, False, cached_tokens=1234)
    # P-10：脚本进程无刷盘线程，入队后须显式排空缓冲，跨进程（服务端读取）才可见
    flush_now()
    after = req("GET", "/api/admin/ops/metrics", token=h)[1].get("month_cached_tokens", 0)
    check("M5 cached_tokens 落库并计入月度汇总", after - before == 1234,
          f"before={before} after={after} delta={after - before}")
    st, bd = req("GET", "/api/admin/usage/breakdown", token=h)
    m5row = next((b for b in bd if b.get("key") == "m5-test-model"), None)
    check("M5 用量明细含测试记录", bool(m5row), f"{m5row}")

    # ============ 汇总 ============
    print("\n" + "=" * 60)
    ok = sum(1 for _, p, _ in results if p)
    print(f"M1–M7 全流程测试：{ok}/{len(results)} 通过")
    for name, p, detail in results:
        if not p:
            print(f"  [FAIL] {name} — {detail}")
    print("=" * 60)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
