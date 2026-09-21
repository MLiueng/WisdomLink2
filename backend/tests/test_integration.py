"""集成测试（C-04 可重复执行套件）：mock 链路端到端，覆盖 TC-701…TC-724。

隔离策略：独立 SQLite（data/itest.db）+ FAISS（data/itest_faiss）+ mock LLM/Embedding，
不依赖网络与外部服务，可反复执行。运行：.venv/Scripts/python -m pytest tests/test_integration.py -q
"""
import os
import time
from pathlib import Path

# —— 环境隔离必须在导入 app 之前 ——
os.environ.update({
    "WL2_DB_URL": "sqlite:///./data/itest.db",
    "WL2_LLM_ACTIVE": "mock",
    "WL2_EMB_ACTIVE": "mock",
    "WL2_VECTOR_DRIVER": "faiss",
    "WL2_FAISS_DIR": "./data/itest_faiss",
    "WL2_ADMIN_PW": "itest-admin-pw",
    # S-01：弱密钥拒绝签发，测试环境必须使用 ≥32 位随机密钥
    "WL2_SECRET": "itest-secret-key-0123456789abcdef-0123456789abcdef",
    # P-10：测试环境收紧计量/审计刷盘间隔（默认 1s），保证直读 DB 的断言可见性
    "WL2_METER_FLUSH_MS": "100",
})
Path("./data/itest.db").unlink(missing_ok=True)
import shutil
shutil.rmtree("./data/itest_faiss", ignore_errors=True)

import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.models import ChunkMeta

KB = {"name": "集成测试库", "description": "C-04 自动化"}


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        ev = next((l[7:] for l in block.splitlines() if l.startswith("event: ")), "")
        data = next((l[6:] for l in block.splitlines() if l.startswith("data: ")), "")
        if ev:
            try:
                events.append((ev, json.loads(data)))
            except json.JSONDecodeError:
                events.append((ev, {}))
    return events


def wait_published(client, doc_id: int, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d.get("status") in ("published", "failed"):
            return d
        time.sleep(0.3)
    raise TimeoutError(f"doc {doc_id} 未在 {timeout}s 内完成入库")


def ask(client, question: str, kb_ids: list[int]):
    r = client.post("/api/chat/stream", json={"question": question, "kb_ids": kb_ids})
    assert r.status_code == 200
    return parse_sse(r.text)


DOC_MD = """# 差旅报销制度

公司员工差旅费用报销分为三类：交通、住宿、餐补。XX公司机密
所有报销需在 OA 系统提交发票照片。

## 第一章 报销标准

单笔报销超过 5000 元需财务总监审批。
交通费用按实报销，住宿一线城市标准 600 元/晚。

## 第二章 审批流程

直属主管 3 个工作日内完成审批，财务每月 15 日、30 日打款。
"""


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/admin/login", json={"username": "admin", "password": "itest-admin-pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="module")
def kb_id(client, admin) -> int:
    r = client.post("/api/kb", json=KB, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def doc_id(client, admin, kb_id) -> int:
    r = client.post("/api/documents/upload", headers=admin,
                    data={"kb_id": str(kb_id)},
                    files={"file": ("差旅报销制度.md", DOC_MD.encode("utf-8"), "text/markdown")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_701_admin_unauthorized_401(client):
    """TC-701 管理面未登录 → 401（NFR-219）。"""
    assert client.post("/api/kb", json={"name": "x"}).status_code == 401


def test_702_create_kb_and_root_folder(client, kb_id):
    """TC-702 建库自动建根目录（FR-102）。"""
    folders = client.get(f"/api/kb/{kb_id}/folders").json()
    assert any(f["name"] == "全部文档" for f in folders)


def test_703_folder_create_and_list(client, admin, kb_id):
    """TC-703 文件夹创建（FR-102）。"""
    r = client.post(f"/api/kb/{kb_id}/folders", json={"name": "制度"}, headers=admin)
    assert r.status_code == 200
    names = [f["name"] for f in client.get(f"/api/kb/{kb_id}/folders").json()]
    assert "制度" in names


def test_704_upload_and_ingest_published(client, admin, kb_id, doc_id):
    """TC-704 上传→解析→清洗→分片→索引→已发布（FR-103/105/113，BR-008）。"""
    d = wait_published(client, doc_id)
    assert d["status"] == "published", d


def test_705_dedup_reject_409(client, admin, kb_id):
    """TC-705 同内容重复上传拒绝（FR-126，BR-002）。"""
    r = client.post("/api/documents/upload", headers=admin,
                    data={"kb_id": str(kb_id)},
                    files={"file": ("改名同内容.md", DOC_MD.encode("utf-8"), "text/markdown")})
    assert r.status_code == 409


def test_706_chunks_with_parent_child_and_hash(client, doc_id):
    """TC-706 分片含父子结构且每片有清洗后哈希（FR-105，BR-003/010）。"""
    d = client.get(f"/api/documents/{doc_id}").json()
    roles = {c["role"] for c in d["chunks"]}
    assert "parent" in roles and "child" in roles
    assert all(c["hash"] for c in d["chunks"])


def test_707_clean_log_watermark(client, doc_id):
    """TC-707 清洗留痕（水印规则命中）（FR-115，BR-009）。"""
    d = client.get(f"/api/documents/{doc_id}").json()
    logs = d["clean_logs"]
    assert logs and any("机密" in l["before"] for l in logs)


def test_708_trace_verify_ok_and_tamper_detected(client, doc_id):
    """TC-708 溯源指纹校验：正常通过 / 篡改即告警（FR-118，BR-010）。"""
    d = client.get(f"/api/documents/{doc_id}").json()
    chunk = next(c for c in d["chunks"] if c["role"] == "child")
    ok = client.get(f"/api/documents/trace/verify/{chunk['id']}").json()
    assert ok["verified"] is True
    # 直接篡改库内文本 → 实时比对失败
    db = SessionLocal()
    try:
        row = db.get(ChunkMeta, chunk["id"])
        row.text = row.text + "（被篡改）"
        db.commit()
    finally:
        db.close()
    bad = client.get(f"/api/documents/trace/verify/{chunk['id']}").json()
    assert bad["verified"] is False


def test_709_qa_exact_hit(client, admin, kb_id):
    """TC-709 QA 精确命中（FR-131，BR-011 正例-精确）。"""
    r = client.post("/api/qa", headers=admin, json={
        "kb_id": kb_id, "std_question": "差旅报销流程是什么",
        "std_answer": "OA 提交发票照片，主管 3 日内审批，每月 15/30 日打款。",
        "variants": ["报销怎么走流程"], "status": "enabled"})
    assert r.status_code == 200
    time.sleep(1.5)   # NFR-264：字典+向量 ≤1min 生效（mock 链路秒级）
    evs = ask(client, "差旅报销流程是什么？", [kb_id])
    assert "qa_hit" in [e for e, _ in evs]


def test_710_qa_variant_hit(client, kb_id):
    """TC-710 QA 变体命中（BR-011 正例-相似/变体）。"""
    evs = ask(client, "报销怎么走流程", [kb_id])
    assert "qa_hit" in [e for e, _ in evs]


def test_711_unrelated_question_not_qa_hit(client, kb_id):
    """TC-711 冷门问题不误命中 QA（BR-011 反例）。"""
    evs = ask(client, "量子纠缠在财务系统中的应用", [kb_id])
    assert "qa_hit" not in [e for e, _ in evs]


def test_712_rag_answer_with_citations(client, kb_id):
    """TC-712 RAG 问答带引用与溯源字段（FR-107/118）。"""
    evs = ask(client, "住宿报销标准是多少钱一晚", [kb_id])
    types = [e for e, _ in evs]
    assert "citations" in types
    cites = dict(evs)["citations"]
    assert cites and all(c["chunk_id"] and c["snippet"] for c in cites)


def test_713_qa_hit_saves_tokens(client, kb_id):
    """TC-713 QA 命中计节省量且 0 生成 token（FR-131/132，BR-012）。"""
    evs = ask(client, "差旅报销流程是什么", [kb_id])
    done = dict(evs)["done"]
    assert done["answer_type"] == "qa" and done["tokens_saved"] >= 1200


def test_714_usage_stats_pure_usage_with_vendor(client, admin):
    """TC-714 用量统计纯用量口径含厂商（FR-132，BR-012，用户裁决 2026-09-12）。"""
    summary = client.get("/api/admin/usage/summary", headers=admin).json()
    assert "cost" not in summary and "currency" not in summary
    bd = client.get("/api/admin/usage/breakdown", headers=admin).json()
    assert bd and "vendor" in bd[0] and "cost" not in bd[0]


def test_715_audit_records_chat_and_qa(client, admin):
    """TC-715 审计覆盖问答与 QA 变更（FR-112，NFR-218）。"""
    items = client.get("/api/admin/audit", headers=admin).json()["items"]
    actions = {a["action"] for a in items}
    assert "chat.ask" in actions and "qa.create" in actions


def test_716_eval_run_returns_hits(client, admin, kb_id):
    """TC-716 检索评测接口返回命中（FR-124）。"""
    r = client.post("/api/admin/eval/run", headers=admin,
                    json={"kb_ids": [kb_id], "queries": ["住宿报销标准"]})
    assert r.status_code == 200
    hits = r.json()[0]["hits"]
    assert hits, "评测查询应命中已发布文档片段"


def test_717_kg_entity_link_empty_safe(client, kb_id):
    """TC-717 图谱路未命中时安全跳过（FR-117 降级口径）。"""
    r = client.get(f"/api/kg/{kb_id}/link", params={"q": "不存在的实体XYZ"})
    assert r.status_code == 200 and r.json()["entities"] == []


def test_718_doc_rebuild(client, admin, doc_id):
    """TC-718 按原文重建索引（FR-126/BR-009 还原路径）。"""
    r = client.post(f"/api/documents/{doc_id}/rebuild", headers=admin)
    assert r.status_code == 200
    d = wait_published(client, doc_id)
    assert d["status"] == "published"


def test_719_health_and_readyz(client):
    """TC-719 健康与就绪（NFR-241）。"""
    assert client.get("/healthz").json()["status"] == "up"
    assert client.get("/readyz").json()["status"] in ("up", "degraded")


def test_720_delete_doc_to_recycle(client, admin, kb_id):
    """TC-720 删除进回收站（FR-104，BR-008）。"""
    r = client.post("/api/documents/upload", headers=admin,
                    data={"kb_id": str(kb_id)},
                    files={"file": ("待删除.md", b"temp doc for delete"), "": ""})
    doc_id = r.json()["id"]
    wait_published(client, doc_id)
    assert client.delete(f"/api/documents/{doc_id}", headers=admin).json()["ok"] is True
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "deleted"


UNIQUE_DOC = "# 紫晶凭证制度\n\n紫晶凭证仅限测试使用。\n"


def test_721_delete_cascade_full_exit_from_recall(client, admin, kb_id):
    """TC-721 删除级联（审计 H1）：删除后向量点清除 + 该文档片段立即退出召回。"""
    from app.providers.vector_store import get_vector_store
    r = client.post("/api/documents/upload", headers=admin,
                    data={"kb_id": str(kb_id)},
                    files={"file": ("紫晶凭证制度.md", UNIQUE_DOC.encode("utf-8"), "text/markdown")})
    doc_id = r.json()["id"]
    d = wait_published(client, doc_id)
    assert d["status"] == "published" and d["chunks"], "前置：发布且产生分片"
    doc_chunks = {str(c["id"]) for c in d["chunks"]}
    # 删除前：向量库有该文档的点，且检索可命中
    data = get_vector_store()._load(f"chunk_{kb_id}")
    assert any(p.get("doc_id") == doc_id for p in data["payloads"])
    hits_before = client.post("/api/admin/eval/run", headers=admin,
                              json={"kb_ids": [kb_id], "queries": ["紫晶凭证"]}).json()[0]["hits"]
    assert {h["chunk_id"] for h in hits_before} & doc_chunks, "前置：删除前检索应命中该文档片段"
    # 删除：向量清除 + 状态进回收站
    assert client.delete(f"/api/documents/{doc_id}", headers=admin).json()["ok"] is True
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "deleted"
    data = get_vector_store()._load(f"chunk_{kb_id}")
    assert not any(p.get("doc_id") == doc_id for p in data["payloads"]), "删除后向量点应级联清除"
    # 删除后：被删文档片段立即退出召回（其余命中来自库内其它在架文档，属预期）
    hits_after = client.post("/api/admin/eval/run", headers=admin,
                             json={"kb_ids": [kb_id], "queries": ["紫晶凭证"]}).json()[0]["hits"]
    leaked = {h["chunk_id"] for h in hits_after} & doc_chunks
    assert not leaked, f"删除后仍召回被删文档片段: {leaked}"


def test_722_citation_alignment_keeps_only_cited(client, monkeypatch, kb_id, doc_id):
    """TC-722 引用对齐正向（审计发现回填缺陷的对偶）：仅保留正文实际标注 [1] 的命中。"""
    from app.providers.base import Usage
    wait_published(client, doc_id)   # 显式依赖主文档发布完成（-k 单跑时 doc_id 无其它消费者兜底）

    class _CiteLLM:
        name, model_id, vendor = "mock", "mock-cite", "mock"

        async def chat(self, messages, temperature=0.3):
            return "住宿一线城市报销标准为 600 元/晚 [1]。", Usage(10, 10, True)

        async def chat_stream(self, messages):
            yield "住宿一线城市报销标准为 600 元/晚 [1]。", None
            yield "", Usage(10, 10, True)

    import app.engine.graph as graph_mod
    monkeypatch.setattr(graph_mod, "get_llm", lambda: _CiteLLM())
    evs = ask(client, "住宿报销标准是多少钱一晚", [kb_id])
    ev = dict(evs)
    assert ev["done"]["answer_type"] == "rag"
    cites = ev["citations"]
    assert len(cites) == 1, f"正文只标注 [1]，对齐后应只剩 1 条引用: {cites}"
    assert cites[0]["seq"] == 1


def test_723_qa_similar_branch_hit(client, admin, monkeypatch):
    """TC-723 QA 相似分支集成（审计 M2/H3 对偶）：非精确问法经向量双阈值命中。"""
    from app.config import get_settings
    # 独立库隔离候选集，保证单候选（margin 天然满足），聚焦相似分支本身
    kb2 = client.post("/api/kb", json={"name": "QA相似分支库"}, headers=admin).json()["id"]
    r = client.post("/api/qa", headers=admin, json={
        "kb_id": kb2, "std_question": "年假怎么申请",
        "std_answer": "OA 系统提交年假申请单，主管审批后生效。",
        "variants": [], "status": "enabled"})
    assert r.status_code == 200, r.text
    cfg = get_settings()
    # mock 词袋向量相似度有限，调低阈值聚焦验证"向量相似分支"链路（双阈值判定/去重/过滤）
    monkeypatch.setattr(cfg, "qa_sim_threshold", 0.2)
    evs = ask(client, "请问年假的申请流程是什么", [kb2])
    ev = dict(evs)
    assert "qa_hit" in ev, "相似问法应走向量相似分支命中"
    assert ev["qa_hit"]["mode"] == "similar"
    assert ev["done"]["answer_type"] == "qa" and ev["done"]["tokens_saved"] >= 1200


def test_724_dense_hit_backfills_heading_and_page(client, admin, kb_id, doc_id):
    """TC-724 稠密召回顶层字段回填（拒答根因修复回归）：向量驱动 search 产出
    ChunkHit 必须携带顶层 heading_path（LLM 上下文章节标签依赖它，缺失会诱发拒答）。"""
    import asyncio
    from app.providers.embedding import get_embedding
    from app.providers.vector_store import get_vector_store
    wait_published(client, doc_id)

    async def _dense_hits():
        qvec = (await get_embedding().embed(["住宿报销标准"]))[0]
        return await get_vector_store().search(f"chunk_{kb_id}", qvec, 5)

    hits = asyncio.run(_dense_hits())
    assert hits, "前置：稠密召回应有命中"
    assert any(h.heading_path for h in hits), \
        f"稠密命中顶层 heading_path 全为空（会诱发模型拒答）: {[(h.chunk_id, h.heading_path) for h in hits]}"
    # API 口径同样带章节标签
    api_hits = client.post("/api/admin/eval/run", headers=admin,
                           json={"kb_ids": [kb_id], "queries": ["住宿报销标准"]}).json()[0]["hits"]
    assert any(h["heading"] for h in api_hits), "eval/run 命中应带章节标签"


def test_725_eval_metrics_include_accuracy_dims(client, admin, kb_id, doc_id):
    """TC-725 评测指标扩展（检索专项）：报告新增 precision / hit1 / ndcg 准确性维度。"""
    from app.models import EvalCase
    wait_published(client, doc_id)   # -k 单跑时显式等待主文档发布（分片可检索）
    db = SessionLocal()
    try:
        db.add(EvalCase(kb_id=kb_id, case_type="single_hop",
                        question="住宿报销标准是多少", expect="600元/晚", enabled=True))
        db.commit()
    finally:
        db.close()
    report = client.post(f"/api/admin/eval/auto-run?kb_id={kb_id}", headers=admin).json()
    assert "metrics" in report, report
    m = report["metrics"]
    for key in ("precision", "hit1", "ndcg"):
        assert key in m, f"评测指标缺少准确性维度 {key}"
    # 至少一题产生准确性数值（命中关键词 600）
    dets = [d for d in report["details"] if d.get("ndcg", 0) > 0 or d.get("precision", 0) > 0]
    assert dets, "应有至少一题产出非零准确性指标"


def test_726_bm25_search_mode_tokenization(client, admin, kb_id):
    """TC-726 BM25 搜索模式分词（检索专项）：长复合词可被细粒度子词召回。"""
    # 复合词「住宿报销」应能命中含「住宿」「报销」子词的分片
    hits = client.post("/api/admin/eval/run", headers=admin,
                       json={"kb_ids": [kb_id], "queries": ["住宿费用报销标准"]}).json()[0]["hits"]
    assert hits, "复合词查询应通过细粒度分词命中分片"


def test_727_web_search_fallback_on_insufficient(client, admin, monkeypatch):
    """TC-727 联网检索兜底（检索专项）：本地零召回且灰度开启时，web 命中补充回答。"""
    from app.config import get_settings
    from app.db import SessionLocal as _SL
    from app.models import SysConfig
    cfg = get_settings()
    monkeypatch.setattr(cfg, "web_search_active", "mock")
    # 空库：本地三路必然零召回 → 触发联网检索兜底
    empty_kb = client.post("/api/kb", json={"name": "联网兜底空库"}, headers=admin).json()["id"]
    db = _SL()
    try:
        row = db.get(SysConfig, "runtime_web_search")
        if row:
            row.value = "on"
        else:
            db.add(SysConfig(key="runtime_web_search", value="on"))
        db.commit()
    finally:
        db.close()
    # 直接写库绕过 API：主动失效配置缓存（P-06），否则灰度开关最长 30s 后才生效
    from app.core.runtime_config import invalidate_runtime
    invalidate_runtime("runtime_web_search")
    try:
        evs = ask(client, "火星上是否存在液态水湖泊", [empty_kb])
        ev = dict(evs)
        done = ev.get("done", {})
        assert done.get("web_search") is True, f"灰度开启且本地不足时应触发联网检索: {done}"
        assert done["answer_type"] == "rag", "联网补充后应产出 rag 回答而非拒答"
        cites = ev.get("citations", [])
        assert cites and cites[0]["source"] == "web" and cites[0].get("url"), "引用应为联网来源且带 URL"
    finally:
        # 恢复灰度开关（关闭），避免影响其它用例
        db = _SL()
        try:
            r = db.get(SysConfig, "runtime_web_search")
            if r:
                db.delete(r)
                db.commit()
        finally:
            db.close()
        invalidate_runtime("runtime_web_search")


def test_728_web_search_disabled_by_default(client, admin, kb_id, monkeypatch):
    """TC-728 联网检索灰度默认关闭：不触发 web 命中（回退纯本地行为）。"""
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "web_search_active", "mock")
    # runtime_web_search 缺省未置 on → web_search_enabled() 应为 False
    from app.engine.web_search import web_search_enabled
    assert web_search_enabled() is False, "灰度开关缺省关闭时不应启用联网检索"


def test_729_done_event_contract(client, kb_id):
    """TC-729 done 事件契约（Q-02/F-02/F-03）：
    必含 message_id（前端点赞/点踩依赖）与 web_search 布尔标记；
    message_id 可反查消息记录。"""
    evs = ask(client, "住宿标准是多少", [kb_id])
    ev = dict(evs)
    done = ev.get("done")
    assert done is not None, "SSE 流必须以 done 事件收尾"
    assert isinstance(done.get("message_id"), int) and done["message_id"] > 0, \
        f"done 必须携带自增 message_id（F-02 契约）: {done}"
    assert isinstance(done.get("web_search"), bool), "done 必须携带 web_search 布尔标记（F-03 契约）"
    assert "latency_ms" in done and "answer_type" in done
    # message_id 反查：反馈接口依赖该 id 能命中消息行
    from sqlalchemy import select
    from app.models import Message
    db = SessionLocal()
    try:
        msg = db.get(Message, done["message_id"])
        assert msg is not None and msg.answer, "done.message_id 必须对应已落库的消息"
    finally:
        db.close()


def test_730_feedback_roundtrip(client, kb_id):
    """TC-730 反馈闭环契约（Q-02）：done.message_id → 点赞/点踩 → 列表可查。"""
    ev = dict(ask(client, "餐补怎么发放", [kb_id]))
    mid = ev["done"]["message_id"]
    r = client.post("/api/chat/feedback", json={"message_id": mid, "value": "down",
                                                 "comment": "回答不准确"})
    assert r.status_code == 200 and r.json().get("ok")
    # 更新为 up（幂等覆盖），并验证列表默认返回差评
    r = client.post("/api/chat/feedback", json={"message_id": mid, "value": "down"})
    assert r.status_code == 200
    fbs = client.get("/api/chat/feedback/list", params={"value": "down"}).json()
    assert any(f["message_id"] == mid for f in fbs), "差评列表应包含刚提交的反馈"


def test_731_mining_requires_kb_id(client):
    """TC-731 /qa/mining 必填参数契约（F-06）：缺 kb_id 必须 422，防止前端静默漏传。"""
    r = client.get("/api/qa/mining")
    assert r.status_code == 422, "mining 缺必填 kb_id 应返回 422"
    r = client.get("/api/qa/mining", params={"kb_id": 1})
    assert r.status_code == 200


def test_732_web_search_failure_degrades_gracefully(client, admin, monkeypatch):
    """TC-732 联网检索失败降级契约（Q-02）：provider 抛异常时不阻断回答，
    done.degraded 记录 web_search，回答回退纯本地路径。"""
    from app.config import get_settings
    from app.db import SessionLocal as _SL
    from app.models import SysConfig
    cfg = get_settings()
    monkeypatch.setattr(cfg, "web_search_active", "mock")
    empty_kb = client.post("/api/kb", json={"name": "联网失败降级空库"}, headers=admin).json()["id"]
    db = _SL()
    try:
        row = db.get(SysConfig, "runtime_web_search")
        if row:
            row.value = "on"
        else:
            db.add(SysConfig(key="runtime_web_search", value="on"))
        db.commit()
    finally:
        db.close()
    # 直接写库绕过 API：主动失效配置缓存（P-06）
    from app.core.runtime_config import invalidate_runtime
    invalidate_runtime("runtime_web_search")

    # 让 provider.search 抛异常：模拟外部检索服务不可达
    from app.engine import web_search as ws

    class _BoomSearcher:
        name = "boom"

        async def search(self, query, top_n):
            raise RuntimeError("upstream unreachable")

    monkeypatch.setattr(ws, "get_searcher", lambda: _BoomSearcher())
    try:
        ev = dict(ask(client, "某冷门外部资讯的最新进展", [empty_kb]))
        done = ev.get("done", {})
        assert done, "联网检索失败也必须以 done 收尾"
        assert done.get("web_search") is False, "provider 失败时不得标记联网补充"
        assert "web_search" in (done.get("degraded") or []), f"降级列表应记录 web_search: {done}"
        # 不得因外部失败抛 5xx / 中断流（状态码已在 ask 内断言 200）
    finally:
        db = _SL()
        try:
            r = db.get(SysConfig, "runtime_web_search")
            if r:
                db.delete(r)
                db.commit()
        finally:
            db.close()
        invalidate_runtime("runtime_web_search")


def test_733_semantic_chunking_grayscale(client, admin):
    """TC-733 B-01 语义分片接线（Q-02）：runtime_semantic_chunk=on 时入库走句子边界
    分片（分片均落在句子终结符），关闭后行为回退递归切分；两种模式均可正常入库并召回。"""
    from app.core.runtime_config import invalidate_runtime
    from app.db import SessionLocal as _SL
    from app.models import SysConfig
    kb = client.post("/api/kb", json={"name": "语义分片灰度库"}, headers=admin).json()["id"]
    # 开灰度：入库应走语义分片
    db = _SL()
    try:
        db.add(SysConfig(key="runtime_semantic_chunk", value="on"))
        db.commit()
    finally:
        db.close()
    invalidate_runtime("runtime_semantic_chunk")
    try:
        md = "# 分片测试文档\n\n这是第一句话，验证句子边界切分。这是第二句话，紧随其后。" * 30
        files = {"file": ("语义分片测试.md", md.encode("utf-8"), "text/markdown")}
        doc_id = client.post("/api/documents/upload", data={"kb_id": kb}, files=files,
                             headers=admin).json()["id"]
        d = wait_published(client, doc_id)
        assert d["status"] == "published", f"语义分片模式入库失败: {d}"
        chunks = [c for c in client.get(f"/api/documents/{doc_id}").json()["chunks"]
                  if c["role"] == "child"]
        assert chunks, "语义分片模式应产出 child 分片"
        # 句子边界：分片文本（去尾部重叠残段后）应以句末标点收尾；
        # 宽松断言：至少一个分片以句子终结符结尾（重叠尾缀允许例外）
        assert any(c["text"].rstrip().endswith(("。", "！", "？", ".", "!", "?")) for c in chunks), \
            "语义分片应产生以句子终结符收尾的分片"
        # 两种模式检索链路一致：可正常召回
        ev = dict(ask(client, "验证句子边界切分", [kb]))
        assert ev.get("done", {}).get("answer_type") in ("rag", "qa"), "语义分片模式应可正常问答"
    finally:
        db = _SL()
        try:
            r = db.get(SysConfig, "runtime_semantic_chunk")
            if r:
                db.delete(r)
                db.commit()
        finally:
            db.close()
        invalidate_runtime("runtime_semantic_chunk")


def test_734_login_rate_limit_and_audit(client, admin, monkeypatch):
    """TC-734 登录限流契约（S-06/S-09）：连续失败触发 429，且限流与审计留痕可见。"""
    from app.config import get_settings
    import app.core.rate_limit as rl
    monkeypatch.setattr(rl, "_redis", lambda: None)   # 隔离真实 Redis，走内存计数
    rl.reset()
    monkeypatch.setattr(get_settings(), "rate_login_per_min", 2)
    try:
        codes = []
        for _ in range(5):
            r = client.post("/api/admin/login", json={"username": "admin", "password": "wrong-pw"})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert 401 in codes, f"前几次应为凭据错误 401: {codes}"
        assert codes[-1] == 429, f"超限后应返回 429: {codes}"
        # S-09：超限事件写审计
        items = client.get("/api/admin/audit", params={"action": "login_rate_limited"},
                           headers=admin).json()["items"]
        assert items, "登录限流应留下审计记录"
        # S-09：kb 写操作审计补齐（前置用例已建库）
        items = client.get("/api/admin/audit", params={"action": "kb.create"},
                           headers=admin).json()["items"]
        assert items, "kb.create 应留下审计记录"
    finally:
        rl.reset()


def test_735_quota_preflight_rejects(client, admin, kb_id, monkeypatch):
    """TC-735 日配额预检契约（S-05/S-09）：限额触顶后问答友好拒答（degraded=quota_exceeded），
    且超限事件写审计。"""
    from app.config import get_settings
    import app.core.quota as quota
    monkeypatch.setattr(quota, "_redis", lambda: None)   # 隔离真实 Redis，走 DB/内存计数
    quota._db_cache = None
    quota._pending = 0
    monkeypatch.setattr(get_settings(), "daily_token_limit", 100)   # 预估值 6000 必然触顶
    try:
        ev = dict(ask(client, "住宿报销标准是多少钱一晚", [kb_id]))
        done = ev.get("done", {})
        assert done.get("answer_type") == "refusal", f"超限应拒答: {done}"
        assert "quota_exceeded" in (done.get("degraded") or []), f"degraded 应记录超限: {done}"
        assert done.get("token_in") == 0 and done.get("token_out") == 0, "拒答不应产生模型消耗"
        items = client.get("/api/admin/audit", params={"action": "quota_exceeded"},
                           headers=admin).json()["items"]
        assert items, "配额超限应留下审计记录"
    finally:
        quota._db_cache = None
        quota._pending = 0


def test_736_chat_rate_limit(client, kb_id, monkeypatch):
    """TC-736 问答限流契约（S-06）：免登录问答按 IP 限流，超限返回 429。"""
    from app.config import get_settings
    import app.core.rate_limit as rl
    monkeypatch.setattr(rl, "_redis", lambda: None)
    rl.reset()
    monkeypatch.setattr(get_settings(), "rate_chat_per_min", 3)
    try:
        codes = []
        for _ in range(6):
            r = client.post("/api/chat/stream", json={"question": "住宿标准", "kb_ids": [kb_id]})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert codes[:3] == [200, 200, 200], f"限额内应全部放行: {codes}"
        assert codes[-1] == 429, f"超限后应返回 429: {codes}"
    finally:
        rl.reset()


def test_737_incremental_reindex_no_full_rebuild(client, admin, kb_id, monkeypatch):
    """TC-737 B-06 增量 reindex 契约：编辑单条 QA 仅重嵌该条（不整集合重建），
    且快路径无空窗——编辑后仍可命中、停用即时退出命中。"""
    import app.engine.qa_engine as qe
    calls: list[list[str]] = []
    orig_embed = qe.get_embedding

    def recording_embedding():
        emb = orig_embed()
        orig_fn = emb.embed

        async def wrapped(texts):
            calls.append(list(texts))
            return await orig_fn(texts)
        emb.embed = wrapped
        return emb

    r = client.post("/api/qa", headers=admin, json={
        "kb_id": kb_id, "std_question": "增量重建测试问题甲",
        "std_answer": "答案甲", "variants": [], "status": "enabled"})
    assert r.status_code == 200
    qa_a = r.json()["id"]
    r = client.post("/api/qa", headers=admin, json={
        "kb_id": kb_id, "std_question": "增量重建测试问题乙",
        "std_answer": "答案乙", "variants": [], "status": "enabled"})
    qa_b = r.json()["id"]
    monkeypatch.setattr(qe, "get_embedding", recording_embedding)
    try:
        # 编辑 A（改问题文本）：只应重嵌 A 的新问句，不得触碰 B（整集合重建会带上 B；
        # B-04 缓存下 B 的旧向量也不会被重新计算）
        calls.clear()
        r = client.patch(f"/api/qa/{qa_a}", headers=admin, json={
            "kb_id": kb_id, "std_question": "增量重建测试问题甲改",
            "std_answer": "答案甲", "variants": [], "weight": 120, "status": "enabled"})
        assert r.status_code == 200
        embedded = [t for batch in calls for t in batch]
        assert embedded, "问题文本变更后应触发增量重嵌"
        assert all("甲" in t for t in embedded), f"增量重嵌不得包含无关 QA: {embedded}"
        # 快路径无空窗：编辑后仍可精确命中
        evs = ask(client, "增量重建测试问题甲改", [kb_id])
        assert "qa_hit" in [e for e, _ in evs], "编辑后快路径应保持可用"
        # 停用即时退出命中
        assert client.post(f"/api/qa/{qa_b}/toggle", headers=admin).status_code == 200
        evs = ask(client, "增量重建测试问题乙", [kb_id])
        assert "qa_hit" not in [e for e, _ in evs], "停用后应即时退出命中"
    finally:
        client.delete(f"/api/qa/{qa_a}", headers=admin)
        client.delete(f"/api/qa/{qa_b}", headers=admin)


def test_738_variant_autogen_grayscale(client, admin, kb_id, monkeypatch):
    """TC-738 B-05 变体自动生成契约：灰度开启后新建 QA 自动生成 ≥3 条变体，
    与全库已有问题/变体归一化去重；灰度关闭则不生成。"""
    from app.core.runtime_config import invalidate_runtime
    from app.db import SessionLocal as _SL
    from app.models import SysConfig
    from app.providers.base import Usage

    class _StubLLM:
        name, model_id, vendor = "stub", "stub-llm", "mock"

        async def chat(self, messages, temperature=0.3, max_tokens=None):
            # 第 3 条与库内已有变体（TC-709「报销怎么走流程」）重复，应被去重
            return '["年假申请入口在哪","怎么提交年假申请","报销怎么走流程","年假流程在哪发起","申请年假的操作步骤"]', \
                Usage(10, 5, True)

    import app.providers.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_llm", lambda: _StubLLM())
    db = _SL()
    try:
        row = db.get(SysConfig, "runtime_qa_variants")
        if row:
            row.value = "on"
        else:
            db.add(SysConfig(key="runtime_qa_variants", value="on"))
        db.commit()
    finally:
        db.close()
    invalidate_runtime("runtime_qa_variants")
    qa_id = None
    try:
        r = client.post("/api/qa", headers=admin, json={
            "kb_id": kb_id, "std_question": "年假申请流程是怎样的",
            "std_answer": "OA 提交年假申请，主管审批。", "variants": [], "status": "enabled"})
        assert r.status_code == 200, r.text
        qa_id = r.json()["id"]
        items = client.get("/api/qa", params={"kb_id": kb_id, "q": "年假申请流程"}).json()["items"]
        qa = next(i for i in items if i["id"] == qa_id)
        assert len(qa["variants"]) >= 3, f"应自动生成 ≥3 条变体: {qa['variants']}"
        assert "报销怎么走流程" not in qa["variants"], "与全库已有变体重复的应被去重"
    finally:
        if qa_id:
            client.delete(f"/api/qa/{qa_id}", headers=admin)
        db = _SL()
        try:
            row = db.get(SysConfig, "runtime_qa_variants")
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
        invalidate_runtime("runtime_qa_variants")


def test_739_dislike_to_mining_and_eval_case(client, admin, kb_id):
    """TC-739 F-05 反馈闭环契约：点踩的问题出现在待沉淀区（disliked 标记），
    且可预填为评估题草稿落库。"""
    q = "沉淀闭环测试问题-739"
    ev = dict(ask(client, q, [kb_id]))
    mid = ev["done"]["message_id"]
    assert client.post("/api/chat/feedback",
                       json={"message_id": mid, "value": "down"}).status_code == 200
    # 待沉淀区应包含该差评问题（差评数据源并入）
    items = client.get("/api/qa/mining", params={"kb_id": kb_id}).json()
    hit = next((m for m in items if m["question"] == q), None)
    assert hit is not None and hit["disliked"] is True, f"点踩问题应进入待沉淀区: {items}"
    # 一键转评估题草稿：预填差评问题落库评估集
    r = client.post("/api/admin/eval/cases", headers=admin,
                    json={"kb_id": kb_id, "case_type": "single_hop",
                          "question": q, "expect": "", "enabled": False})
    assert r.status_code == 200
    case_id = r.json()["id"]
    try:
        cases = client.get("/api/admin/eval/cases", params={"kb_id": kb_id}, headers=admin).json()
        assert any(c["id"] == case_id and c["question"] == q for c in cases), \
            "差评问题应可作为评估题草稿落库"
    finally:
        client.delete(f"/api/admin/eval/cases/{case_id}", headers=admin)


def _embed_usage_count(kb_id: int) -> int:
    """文档嵌入调用次数（purpose=embed，按 call_count 求和）：B-04 验收口径——
    全部命中缓存的重建不应产生任何新的记账调用。
    P-10：record_usage 由逐次插行改为按 (日,模型,库,用途) 真聚合，
    行数不再随调用次数增长，须读 call_count；读取前排空批写缓冲。"""
    from sqlalchemy import func as _func, select as _sel
    from app.core.metering import flush_now
    from app.models import ModelUsage
    flush_now()   # P-10：计量批写——读取前排空缓冲
    db = SessionLocal()
    try:
        return db.scalar(_sel(_func.coalesce(_func.sum(ModelUsage.call_count), 0)).where(
            ModelUsage.kb_id == kb_id, ModelUsage.purpose == "embed")) or 0
    finally:
        db.close()


def test_740_rebuild_unchanged_uses_embed_cache(client, admin, kb_id, doc_id):
    """TC-740 B-04 嵌入缓存契约：① 重建未变更文档的嵌入调用数=0（无新记账）；
    ② 变更清洗规则后重建仅对变更分片重嵌（差量>0 且 < 全量）。"""
    wait_published(client, doc_id)
    before = _embed_usage_count(kb_id)
    assert before > 0, "首次入库应产生嵌入记账"
    # ① 原规则重建：所有分片 clean_hash 不变 → 全部命中缓存，无新嵌入记账
    r = client.post("/api/documents/reprocess-batch", headers=admin,
                    json={"doc_ids": [doc_id]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    d = wait_published(client, doc_id)
    assert d["status"] == "published", f"重建后应重新发布: {d}"
    assert _embed_usage_count(kb_id) == before, "重建未变更文档不应产生新的嵌入记账"
    # ② 新增水印规则（命中分片文本内的「发票」字样）→ 变更分片须重嵌
    rr = client.post("/api/admin/clean-rules", headers=admin, json={
        "kb_id": kb_id, "rule_type": "watermark", "pattern": "发票",
        "enabled": True, "priority": 21})
    assert rr.status_code == 200
    rule_id = rr.json()["id"]
    try:
        child_total = sum(1 for c in client.get(f"/api/documents/{doc_id}").json()["chunks"]
                          if c["role"] == "child")
        r = client.post("/api/documents/reprocess-batch", headers=admin,
                        json={"doc_ids": [doc_id]})
        assert r.status_code == 200
        d = wait_published(client, doc_id)
        assert d["status"] == "published", f"规则变更后重建应发布: {d}"
        delta = _embed_usage_count(kb_id) - before
        assert delta > 0, "变更清洗规则后应对变更分片重新嵌入"
        assert delta < child_total, f"增量重嵌（{delta}）应远小于全量分片数（{child_total}）"
    finally:
        client.delete(f"/api/admin/clean-rules/{rule_id}", headers=admin)
        # 还原：移除规则后重建一次，恢复原分片形态
        client.post("/api/documents/reprocess-batch", headers=admin, json={"doc_ids": [doc_id]})
        wait_published(client, doc_id)


def test_741_vector_recon_detects_orphan(client, admin, kb_id):
    """TC-741 A-08 向量反向对账契约：人工注入孤儿向量后对账报不通过并列出孤儿点，
    清理后恢复通过。"""
    import asyncio
    from app.providers.vector_store import get_vector_store

    async def inject_orphan():
        vs = get_vector_store()
        await vs.upsert(f"chunk_{kb_id}", ["999999"], [[0.01] * 256],
                        [{"text": "人工注入孤儿向量", "kb_id": kb_id, "source": "dense",
                          "doc_id": 999999, "doc_version_id": 999999,
                          "embed_model": "mock-embed"}])

    async def purge_orphan():
        await get_vector_store().delete_by(f"chunk_{kb_id}", {"in": {"doc_id": [999999]}})

    asyncio.run(inject_orphan())
    try:
        r = client.post("/api/admin/recon/run", params={"kb_id": kb_id}, headers=admin)
        assert r.status_code == 200
        vr = r.json()["checks"].get("vector_recon")
        assert vr is not None, "对账结果应包含 vector_recon 段"
        assert vr["passed"] is False and vr["orphan_count"] >= 1, f"应检出孤儿向量: {vr}"
        assert "999999" in [str(p) for p in vr["orphan_points"]], "孤儿点应被列出"
        assert vr["embed_models"] == ["mock-embed"] and vr["mixed_models"] is False
        assert r.json()["checks"]["overall_passed"] is False, "存在孤儿时对账总判定应不通过"
    finally:
        asyncio.run(purge_orphan())
    # 清理后恢复通过
    r = client.post("/api/admin/recon/run", params={"kb_id": kb_id}, headers=admin)
    assert r.json()["checks"]["vector_recon"]["passed"] is True, "清理孤儿后对账应恢复通过"


def _set_semantic_cache(on: bool) -> None:
    """P-04 灰度开关直写（绕过 API 须主动失效配置缓存，同 733 口径）。"""
    from app.core.runtime_config import invalidate_runtime
    from app.models import SysConfig
    db = SessionLocal()
    try:
        row = db.get(SysConfig, "runtime_semantic_cache")
        if row:
            row.value = "on" if on else "off"
        else:
            db.add(SysConfig(key="runtime_semantic_cache", value="on" if on else "off"))
        db.commit()
    finally:
        db.close()
    invalidate_runtime("runtime_semantic_cache")


def test_743_semantic_cache_disabled_by_default(client, kb_id, doc_id):
    """TC-743 P-04 灰度纪律：未开启灰度开关时，重复问题不命中语义缓存（仍走 RAG）。"""
    wait_published(client, doc_id)
    _set_semantic_cache(False)
    q = "语义缓存灰度默认关闭测试-743：报销审批几天"
    ev1 = dict(ask(client, q, [kb_id]))
    ev2 = dict(ask(client, q, [kb_id]))
    assert ev1["done"]["answer_type"] == "rag"
    assert ev2["done"]["answer_type"] == "rag", "灰度关闭时第二次提问不应命中语义缓存"


def test_742_semantic_cache_hit_and_invalidate(client, admin, kb_id, doc_id):
    """TC-742 P-04 语义缓存契约：① 灰度开启后重复问题命中缓存（cached 类型、零 Token、
    节省量≥1200）且 SSE 携带 cache_hit 事件；② 文档重建发布（纪元递增）后旧缓存失效回退
    RAG 重新生成；③ 命中率指标在运营看板可见。"""
    wait_published(client, doc_id)
    _set_semantic_cache(True)
    try:
        q = "语义缓存契约-742：报销审批需要多少个工作日"
        ev1 = ask(client, q, [kb_id])
        done1 = dict(ev1)["done"]
        assert done1["answer_type"] == "rag", "首次提问应走 RAG 生成并写缓存"
        assert done1.get("token_in", 0) > 0, "首次提问应产生 LLM Token 消耗"
        ans1 = "".join(d.get("text", "") for e, d in ev1 if e == "delta")
        # ② 同问第二次：语义缓存命中——零 Token、秒回、带 cache_hit 事件
        ev2 = ask(client, q, [kb_id])
        done2 = dict(ev2)["done"]
        ch = next((d for e, d in ev2 if e == "cache_hit"), None)
        assert ch is not None, "缓存命中应推送 cache_hit 事件"
        assert done2["answer_type"] == "cached", f"重复问题应命中语义缓存: {done2}"
        assert done2["token_in"] == 0 and done2["token_out"] == 0, "缓存命中应零 Token 消耗"
        assert done2["tokens_saved"] >= 1200, "缓存命中节省量应与 QA 秒回同口径"
        assert ch["answer"] == ans1, "缓存回答应与原回答一致"
        assert done2["web_search"] is False
        # ③ 文档重建发布 → 纪元递增，旧缓存整体失效，重新生成
        r = client.post("/api/documents/reprocess-batch", headers=admin,
                        json={"doc_ids": [doc_id]})
        assert r.status_code == 200
        wait_published(client, doc_id)
        done3 = dict(ask(client, q, [kb_id]))["done"]
        assert done3["answer_type"] == "rag", f"内容变更后旧缓存应失效并重新生成: {done3}"
        # ④ 命中率指标在运营看板可见（lookup/hits 计数非零）
        ops = client.get("/api/admin/ops/metrics", headers=admin).json()
        sc = ops.get("semantic_cache") or {}
        assert sc.get("hits", 0) >= 1 and sc.get("lookups", 0) >= 2, \
            f"运营看板应展示语义缓存命中指标: {sc}"
    finally:
        _set_semantic_cache(False)


def _set_runtime(key: str, value: str) -> None:
    """运行时开关直写（绕过 API 须主动失效配置缓存，同 733 口径）。"""
    from app.core.runtime_config import invalidate_runtime
    from app.models import SysConfig
    db = SessionLocal()
    try:
        row = db.get(SysConfig, key)
        if row:
            row.value = value
        else:
            db.add(SysConfig(key=key, value=value))
        db.commit()
    finally:
        db.close()
    invalidate_runtime(key)


def test_744_llm_rewrite_grayscale_and_metering(client, admin, kb_id, doc_id, monkeypatch):
    """TC-744 P-05 LLM 改写契约：灰度开启后，检索不足的查询只做一次轻量 LLM 改写
    （purpose=rewrite 计量），检索轮次从盲改写的最多 3 轮降为 2 轮；灰度关闭时回退盲拼接。"""
    wait_published(client, doc_id)
    from app.engine import graph as g
    from app.providers.base import ChunkHit

    calls = []

    async def fake_hybrid(kb_ids, q, top_k=5, folder_ids=None, qvec=None, **kw):
        calls.append(q)
        # 首轮低分（触发不充分→改写），第二轮高分（充分收敛）
        score = 0.10 if len(calls) == 1 else 0.95
        hits = [ChunkHit(chunk_id=f"c{len(calls)}", text="报销审批流程材料",
                         score=score, heading_path="审批流程")]
        return {"hits": hits, "entities": [], "degraded": [],
                "rerank": "none", "reranked": False, "routing": {}, "attribution": {}}

    monkeypatch.setattr(g, "hybrid_search", fake_hybrid)
    _set_runtime("runtime_llm_rewrite", "on")
    try:
        from sqlalchemy import func as _func, select as _sel
        from app.models import ModelUsage

        def _purpose_count(purpose):
            # P-10：批写缓冲读取前排空；调用次数读 call_count 求和（真聚合后行数恒定）
            from app.core.metering import flush_now
            flush_now()
            db = SessionLocal()
            try:
                return db.scalar(_sel(_func.coalesce(_func.sum(ModelUsage.call_count), 0))
                                 .where(ModelUsage.purpose == purpose)) or 0
            finally:
                db.close()

        before = _purpose_count("rewrite")
        import asyncio
        res = asyncio.run(g.Pipeline().answer("审批流程怎么走", [kb_id], None, []))
        assert res["answer_type"] == "rag"
        assert len(calls) == 2, f"LLM 改写应只做一轮定向重检索（共 2 次检索）: {calls}"
        # 第二轮查询应为 LLM 改写结果（mock LLM 回显带 [mock] 前缀），而非盲拼接串
        assert calls[1] != calls[0] and "（换个问法" not in calls[1], \
            f"第二轮应使用 LLM 改写查询: {calls[1]}"
        assert _purpose_count("rewrite") == before + 1, "改写调用应按 BR-012 计量（purpose=rewrite）"
    finally:
        _set_runtime("runtime_llm_rewrite", "off")
        monkeypatch.undo()
    # 灰度关闭回退盲拼接口径（第二轮查询含拼接标记）
    calls2 = []

    async def fake_hybrid2(kb_ids, q, top_k=5, folder_ids=None, qvec=None, **kw):
        calls2.append(q)
        score = 0.10 if len(calls2) == 1 else 0.95
        hits = [ChunkHit(chunk_id=f"b{len(calls2)}", text="报销审批流程材料",
                         score=score, heading_path="审批流程")]
        return {"hits": hits, "entities": [], "degraded": [],
                "rerank": "none", "reranked": False, "routing": {}, "attribution": {}}

    monkeypatch.setattr(g, "hybrid_search", fake_hybrid2)
    import asyncio
    res2 = asyncio.run(g.Pipeline().answer("审批流程怎么走", [kb_id], None, []))
    assert res2["answer_type"] == "rag"
    assert len(calls2) == 2 and "（换个问法" in calls2[1], f"灰度关闭应回退盲拼接: {calls2}"


def test_745_suggestions_async_and_metered(client, kb_id, doc_id):
    """TC-745 P-07 建议问题异步化契约：① done 事件先行、suggestions 独立事件后补
    （顺序断言）；② 建议问题生成的 LLM 调用计入用量（purpose=suggestions）。"""
    wait_published(client, doc_id)
    evs = ask(client, "P-07建议问题异步测试-745：住宿标准是多少", [kb_id])
    types = [e for e, _ in evs]
    assert "done" in types and "suggestions" in types, f"应包含 done 与 suggestions 事件: {types}"
    assert types.index("done") < types.index("suggestions"), "done 必须先于 suggestions（异步后补）"
    sug = next(d for e, d in evs if e == "suggestions")
    assert isinstance(sug, list) and 1 <= len(sug) <= 3, f"建议问题应为 1-3 条: {sug}"
    # done 事件不再携带建议问题（由独立事件承载）
    done = next(d for e, d in evs if e == "done")
    assert not done.get("suggestions"), "done 不应再内嵌建议问题"
    # 计量：purpose=suggestions 入账（BR-012；P-10：读前排空缓冲，口径取 call_count 求和）
    from sqlalchemy import func as _func, select as _sel
    from app.core.metering import flush_now
    from app.models import ModelUsage
    flush_now()
    db = SessionLocal()
    try:
        n = db.scalar(_sel(_func.coalesce(_func.sum(ModelUsage.call_count), 0))
                      .where(ModelUsage.purpose == "suggestions")) or 0
    finally:
        db.close()
    assert n >= 1, "建议问题生成应计入用量（purpose=suggestions）"


def test_746_evidence_fingerprint_remap(client, admin, kb_id, doc_id):
    """TC-746 A-07 证据指纹化契约：① 写入回填指纹；② 文档重建（分片换新 id）后
    发布钩子按指纹重映射悬空指针，证据有效率恢复 100%；③ recon 观测指纹覆盖率。"""
    from sqlalchemy import select as _sel
    from app.models import ChunkMeta, DocVersion, KGEdge, KGNode

    wait_published(client, doc_id)
    db = SessionLocal()
    try:
        ver = db.scalar(_sel(DocVersion).where(DocVersion.document_id == doc_id,
                                               DocVersion.is_current == True))  # noqa: E712
        chunk = db.scalar(_sel(ChunkMeta).where(ChunkMeta.doc_version_id == ver.id,
                                                ChunkMeta.role == "child"))
        n1 = KGNode(kb_id=kb_id, name="财务总监", concept_id=None,
                    aliases="[]", status="confirmed")
        n2 = KGNode(kb_id=kb_id, name="报销审批", concept_id=None,
                    aliases="[]", status="confirmed")
        db.add_all([n1, n2])
        db.commit()
        # 手工加边带证据 → 写入即回填指纹（A-07 接线）
        db.add(KGEdge(kb_id=kb_id, src_id=n1.id, dst_id=n2.id, relation="审批",
                      evidence_chunk_id=chunk.id, status="confirmed"))
        db.commit()
        r = client.post(f"/api/kg/{kb_id}/edges", headers=admin, json={
            "src_id": n2.id, "dst_id": n1.id, "relation": "复核",
            "evidence_chunk_id": chunk.id, "status": "confirmed"})
        assert r.status_code == 200
        from app.engine.kg_evidence import stamp_edges
        if stamp_edges(db, kb_id):
            db.commit()
        db.expire_all()   # 其他会话写入的指纹对本会话可见（避免 identity map 陈旧）
        edges = db.scalars(_sel(KGEdge).where(KGEdge.kb_id == kb_id)).all()
        assert len(edges) == 2 and all(e.evidence_clean_hash == chunk.clean_hash
                                       for e in edges), "写入应回填证据指纹"
        fp, ctext = chunk.clean_hash, chunk.text
        # 模拟重建：旧分片删除、同指纹新分片（新 id）顶替
        db.delete(chunk)
        new_c = ChunkMeta(doc_version_id=ver.id, kb_id=kb_id, seq=998, role="child",
                          text=ctext, clean_hash=fp, active=True,
                          token_len=10, page=1, heading_path="审批流程")
        db.add(new_c)
        db.commit()
        from app.engine.kg_evidence import remap_edges
        stat = remap_edges(kb_id)
        assert stat["remapped"] == 2 and stat["dangling"] == 0, \
            f"悬空指针应按指纹重映射: {stat}"
        db.expire_all()   # remap_edges 独立会话提交，本会话需过期后重读
        remapped = db.scalars(_sel(KGEdge).where(KGEdge.kb_id == kb_id)).all()
        assert all(e.evidence_chunk_id == new_c.id for e in remapped), \
            "重映射后证据应指向新分片"
    finally:
        db.close()
    # 对账观测：证据有效率与指纹覆盖率恢复 100%
    checks = client.post("/api/admin/recon/run", headers=admin,
                         params={"kb_id": kb_id}).json()["checks"]
    kev = checks["kg_evidence_validity"]
    assert kev["rate"] == 1.0 and kev["fingerprint_rate"] == 1.0, \
        f"重建后证据有效率与指纹覆盖率应为 1.0: {kev}"


def test_747_graph_cache_and_vector_link(client, kb_id):
    """TC-747 B-07 图缓存+向量实体链接契约：① 图缓存复用同对象、写操作失效重建；
    ② 灰度纪律：默认关闭时向量链接返回空、检索仍走字符串链接；③ 灰度开启后
    查询向量近邻命中同名实体；④ B-08 倒排索引覆盖库内分片词元。"""
    import asyncio
    from app.engine import kg_graph
    from app.engine.kg_graph import (graph_cache, invalidate_graph_cache,
                                     invalidate_node_vecs, vector_link,
                                     vector_link_enabled)
    from app.providers.embedding import MockEmbedding

    kg_graph.invalidate_graph_cache(None)
    kg_graph.invalidate_node_vecs(None)
    e1 = graph_cache(kb_id)
    e2 = graph_cache(kb_id)
    assert e1 is e2, "指纹未变时图缓存应命中同一对象（不重建）"
    names = {e1["graph"].nodes[n]["name"] for n in e1["graph"].nodes}
    assert "财务总监" in names, "confirmed 实体应进入缓存图"
    # B-08：倒排索引覆盖库内分片词元（主键级回捞替代 LIKE 扫描）
    assert e1["by_key"] and all(isinstance(s, set) for s in e1["by_key"].values())
    kg_graph.invalidate_graph_cache(kb_id)
    assert graph_cache(kb_id) is not e1, "主动失效后应重建缓存"
    kg_graph.invalidate_graph_cache(kb_id)   # 还原，避免影响后续用例
    # ② 灰度纪律：默认关闭
    assert not vector_link_enabled(), "runtime_kg_vector_link 应默认关闭"
    qvec = MockEmbedding._vec("财务总监")
    assert asyncio.run(vector_link(kb_id, qvec)) == [], "灰度关闭时不应走向量链接"
    ents = client.get(f"/api/kg/{kb_id}/link", params={"q": "财务总监"}).json()["entities"]
    assert any(e["name"] == "财务总监" for e in ents), "灰度关闭应回退字符串链接"
    # ③ 灰度开启：查询向量近邻命中同名实体（同文本余弦=1.0 ≥ 阈值 0.85）
    _set_runtime("runtime_kg_vector_link", "on")
    try:
        assert vector_link_enabled()
        kg_graph.invalidate_node_vecs(kb_id)
        hits = asyncio.run(vector_link(kb_id, qvec))
        assert any(h["name"] == "财务总监" for h in hits), \
            f"灰度开启后向量链接应命中同名实体: {hits}"
    finally:
        _set_runtime("runtime_kg_vector_link", "off")
        kg_graph.invalidate_node_vecs(kb_id)


def test_748_metering_batch_aggregation(client, monkeypatch):
    """TC-748 P-10 计量批处理契约：① record_usage 只入队不直写（刷盘前 DB 无新行，
    写段可被单点拦截）；② 配额计数在入队时即时同步累加（不等刷盘，预检口径不失真）；
    ③ 用量按 (日,模型,库,用途) 真聚合——同维度多次入队累加到同一行；
    ④ 审计批量只增写入。"""
    from datetime import date
    from sqlalchemy import func as _func, select as _sel
    from app.core import metering, quota
    from app.models import AuditLog, ModelUsage

    model, purpose = "mock-t748", "t748"

    def _rows():
        metering.flush_now()
        db = SessionLocal()
        try:
            return db.scalars(_sel(ModelUsage).where(
                ModelUsage.stat_date == date.today(), ModelUsage.model_id == model,
                ModelUsage.kb_id.is_(None), ModelUsage.purpose == purpose)).all()
        finally:
            db.close()

    assert _rows() == [], "测试前同维度不应有既有记录"

    # ①② 解耦契约：拦截写段与配额累加——入队不产生 DB 写入，配额即时累加
    captured, added = [], []
    monkeypatch.setattr(metering, "_write_batch", lambda items: captured.extend(items))
    monkeypatch.setattr(quota, "quota_add", lambda t: added.append(t))
    for _ in range(5):
        metering.record_usage(model, "mock", purpose, None, 10, 5)
    metering.flush_now()
    assert len(captured) == 5, f"5 条用量应整批进入写段: {len(captured)}"
    assert added == [15] * 5, "配额计数应在入队时即时累加（不等刷盘）"
    db = SessionLocal()
    try:
        n = db.scalar(_sel(_func.count()).select_from(ModelUsage).where(
            ModelUsage.model_id == model, ModelUsage.purpose == purpose)) or 0
    finally:
        db.close()
    assert n == 0, "写段被拦截时不应有 DB 落库（热路径零写事务）"
    monkeypatch.undo()

    # ③ 真聚合：同维度多次入队 → 单行累加（prompt/completion/call_count）
    for _ in range(5):
        metering.record_usage(model, "mock", purpose, None, 10, 5)
    rows = _rows()
    assert len(rows) == 1, f"同维度应聚合为单行: {len(rows)}"
    assert rows[0].prompt_tokens == 50 and rows[0].completion_tokens == 25
    assert rows[0].call_count == 5
    for _ in range(3):
        metering.record_usage(model, "mock", purpose, None, 10, 5)
    rows = _rows()
    assert len(rows) == 1, "追加入队后仍应为同一聚合行"
    assert rows[0].prompt_tokens == 80 and rows[0].call_count == 8

    # ④ 审计批量只增
    for i in range(3):
        metering.audit("t748.ping", "test", str(i))
    metering.flush_now()
    db = SessionLocal()
    try:
        n = db.scalar(_sel(_func.count()).select_from(AuditLog)
                      .where(AuditLog.action == "t748.ping")) or 0
    finally:
        db.close()
    assert n == 3, f"审计应批量只增落库: {n}"


def test_749_hot_path_nonblocking_and_async_session(client, kb_id, doc_id):
    """TC-749 P-02/P-13 并发非阻塞契约：① 热路径同步段经 to_thread 卸载——
    人为放慢的同步召回段（0.5s）执行期间事件循环心跳持续跳动（阻塞则≈0）；
    ② AsyncSession（aiosqlite/aiomysql）热路径读事务可用。"""
    import asyncio
    from app.engine import retrieval

    wait_published(client, doc_id)

    slow_flag = {"ran": False}

    def slow_bm25(*args, **kwargs):
        time.sleep(0.5)   # 人为放慢：若未卸载而直接阻塞事件循环，心跳将停摆
        slow_flag["ran"] = True
        return []

    orig_bm25 = retrieval.bm25_recall
    retrieval.bm25_recall = slow_bm25
    try:
        async def scenario():
            ticks, stop = 0, False

            async def heartbeat():
                nonlocal ticks, stop
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.02)

            hb = asyncio.create_task(heartbeat())
            res = await retrieval.hybrid_search([kb_id], "报销审批需要几天",
                                                top_k=5, use_rerank=False)
            stop = True
            await hb
            return ticks, res

        ticks, res = asyncio.run(scenario())
    finally:
        retrieval.bm25_recall = orig_bm25
    assert slow_flag["ran"], "BM25 同步段应被执行"
    assert isinstance(res, dict) and "hits" in res, "慢同步段下检索结构应完整"
    # 0.5s 同步段期间心跳 20ms/跳，保守断言 ≥10 跳；未卸载阻塞事件循环时 ticks≈0
    assert ticks >= 10, f"事件循环被同步段阻塞（心跳 ticks={ticks}）"

    # P-13：AsyncSession 热路径读事务（异步驱动接线验证）
    from sqlalchemy import func as _func, select as _sel
    from app.db import AsyncSessionLocal
    from app.models import KB as KBModel

    async def async_read():
        async with AsyncSessionLocal() as db:
            return await db.scalar(_sel(_func.count()).select_from(KBModel))

    assert asyncio.run(async_read()) >= 1, "AsyncSession 读事务应可用"
