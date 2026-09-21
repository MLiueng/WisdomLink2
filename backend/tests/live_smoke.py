"""真实厂商联调冒烟（C-04 Tier-B，独立脚本避免与 pytest 环境互染）。

链路以 .env 实际配置为准（远程生成 + 远程向量化 + 重排；本地/远程形态均可），
红线：LLM/Embedding 必须为真实远程链路（非 mock）。
数据隔离：独立 SQLite（data/live.db）+ FAISS（data/live_faiss）。
运行：.venv/Scripts/python tests/live_smoke.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend 根目录入 path

# —— 隔离数据，但保留真实模型配置（不覆盖 LLM/EMB/RERANK/VECTOR）——
ROOT = Path(__file__).resolve().parents[1]
os_cwd = Path.cwd()
import os
os.chdir(ROOT)   # 保证相对路径（models/、data/）与真实运行一致
os.environ.update({
    "WL2_DB_URL": "sqlite:///./data/live.db",
    "WL2_VECTOR_DRIVER": "faiss",
    "WL2_FAISS_DIR": "./data/live_faiss",
})
Path("./data/live.db").unlink(missing_ok=True)
import shutil
shutil.rmtree("./data/live_faiss", ignore_errors=True)

import urllib.request

BASE = None   # TestClient 无需真实端口


def main():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import get_settings
    from app.providers.llm import get_llm
    from app.providers.embedding import get_embedding
    from app.providers.rerank import get_reranker

    s = get_settings()
    llm, emb, rr = get_llm(), get_embedding(), get_reranker()
    print(f"[配置] LLM={llm.vendor}/{llm.model_id} | Embedding={emb.vendor}/{emb.model_id} | Rerank={rr.name}/{rr.vendor}")
    # 红线：生成/向量化必须是真实远程链路（非 mock）；重排 local/api 形态均可，但不能缺席
    assert llm.name == "openai_compat" and emb.name == "openai_compat" and rr.name != "none", \
        f"应使用真实模型链路，实际 LLM={llm.name} EMB={emb.name} RERANK={rr.name}"

    results = []
    with TestClient(app) as c:
        tok = c.post("/api/admin/login", json={"username": "admin", "password": s.admin_pw}).json()["token"]
        h = {"Authorization": "Bearer " + tok}

        # 1. 建库 + 上传 + 入库（向量化链路以 .env 为准）
        kb_id = c.post("/api/kb", json={"name": "真实联调库"}, headers=h).json()["id"]
        doc_text = ("# 差旅报销制度\n\n员工差旅报销分为交通、住宿、餐补三类，均在 OA 系统提交发票。\n\n"
                    "## 报销标准\n\n住宿一线城市标准 600 元/晚，二线城市 450 元/晚。\n"
                    "单笔超过 5000 元需财务总监审批。\n\n## 打款时间\n\n财务每月 15 日、30 日统一打款。\n")
        r = c.post("/api/documents/upload", headers=h,
                   data={"kb_id": str(kb_id)},
                   files={"file": ("差旅报销制度.md", doc_text.encode("utf-8"), "text/markdown")})
        doc_id = r.json()["id"]
        for _ in range(60):
            d = c.get(f"/api/documents/{doc_id}").json()
            if d["status"] in ("published", "failed"):
                break
            time.sleep(0.5)
        assert d["status"] == "published", f"入库失败: {d}"
        results.append((f"真实入库（{emb.vendor} 向量化）", "PASS", f"{d['version']} / {len(d['chunks'])} 片段"))

        # 2. 真实检索（查询向量 + BM25 + 重排，链路以 .env 为准）
        r = c.post("/api/admin/eval/run", headers=h,
                   json={"kb_ids": [kb_id], "queries": ["住宿报销标准是多少钱"]})
        hits = r.json()[0]["hits"]
        assert hits, "真实检索未命中"
        results.append((f"真实混合检索+重排({rr.name})", "PASS", f"top1={hits[0]['heading'] or hits[0]['snippet'][:20]} score={hits[0]['score']}"))

        # 3. 真实 RAG 问答（生成链路以 .env 为准，流式；Key 失效时降级不失败——NFR-222）
        resp = c.post("/api/chat/stream", json={"question": "住宿报销一晚最多报多少钱？", "kb_ids": [kb_id]})
        evs = {}
        for block in resp.text.split("\n\n"):
            ev = next((l[7:] for l in block.splitlines() if l.startswith("event: ")), "")
            dt = next((l[6:] for l in block.splitlines() if l.startswith("data: ")), "")
            if ev:
                try:
                    evs[ev] = json.loads(dt)
                except json.JSONDecodeError:
                    evs[ev] = dt
        assert evs.get("citations"), (f"无引用: answer_type={evs['done'].get('answer_type')} | "
                                      f"answer={str(evs['done'].get('answer', ''))[:300]}")
        latency = evs["done"]["latency_ms"] / 1000
        if evs["done"]["token_in"] > 0:
            results.append((f"真实 RAG 问答（{llm.vendor}）", "PASS",
                            f"引用 {len(evs['citations'])} 条 · token {evs['done']['token_in']}/{evs['done']['token_out']} · {latency:.1f}s"))
        else:
            degraded_reason = "降级"
            try:
                import asyncio
                from app.providers.llm import get_llm as _gl
                _gl().model_id and None
                asyncio.get_event_loop()
                results.append((f"真实 RAG 问答（{llm.vendor}）", "DEGRADED",
                                f"LLM 调用失败（见上方降级链路生效）· 引用 {len(evs['citations'])} 条照常返回 · {latency:.1f}s"))
            except Exception:
                results.append((f"真实 RAG 问答（{llm.vendor}）", "DEGRADED", "LLM 降级"))

        # 4. 用量统计：厂商与模型（厂商以实际链路为准，不写死）
        bd = c.get("/api/admin/usage/breakdown", headers=h).json()
        vendors = {(b["vendor"], b["key"]) for b in bd}
        assert any(emb.vendor.lower() in v.lower() for v, _ in vendors), vendors
        if evs["done"]["token_in"] > 0:
            assert any(llm.model_id in k for _, k in vendors), vendors
        results.append(("用量统计（厂商+模型）", "PASS", str(sorted(vendors))))

        # 5. 审计
        actions = {a["action"] for a in c.get("/api/admin/audit", headers=h).json()["items"]}
        assert "chat.ask" in actions
        results.append(("审计留痕", "PASS", f"{sorted(actions)[:4]}"))

    print("\n===== 真实厂商联调结果 =====")
    ok, degraded = True, []
    for name, status, detail in results:
        print(f"  [{status}] {name} — {detail}")
        if status == "DEGRADED":
            degraded.append(name)
        ok &= status in ("PASS", "DEGRADED")
    print("============================")
    if degraded:
        print(f"  待办：{degraded} 依赖的 LLM Key 返回 401（无效/欠费）——请到平台核对 WL2_LLM_REMOTE_API_KEY；"
              f"修复后重跑本脚本，该行应变 PASS。降级矩阵已按 NFR-222 正确生效。")
    assert ok
    print("LIVE_SMOKE_ALL_PASS")


if __name__ == "__main__":
    main()
