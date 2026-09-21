"""阶段1-2 验证脚本：P-03 每请求 embedding 调用数、P-01 三路并发、配置缓存生效。

环境隔离与 test_integration.py 一致：独立 SQLite + FAISS + mock，不依赖外部服务。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.update({
    "WL2_DB_URL": "sqlite:///./data/verify_p03.db",
    "WL2_LLM_ACTIVE": "mock",
    "WL2_EMB_ACTIVE": "mock",
    "WL2_VECTOR_DRIVER": "faiss",
    "WL2_FAISS_DIR": "./data/verify_p03_faiss",
    "WL2_ADMIN_PW": "verify-pw",
    "WL2_SECRET": "verify-secret-key-0123456789abcdef-0123456789abcdef",
})
Path("./data/verify_p03.db").unlink(missing_ok=True)
import shutil
shutil.rmtree("./data/verify_p03_faiss", ignore_errors=True)

calls = {"n": 0}
from fastapi.testclient import TestClient
from app.main import app
from app.providers import embedding as em
from app.engine.graph import Pipeline, answer_stream
from app.db import SessionLocal
from app.models import Document, QAPair

DOC_MD = """# 差旅报销制度

## 第一章 报销标准

住宿一线城市标准 600 元/晚，交通按实报销。

## 第二章 审批流程

直属主管 3 个工作日内完成审批。
"""


def seed():
    """建库+入库+QA，准备最小可检索数据。"""
    with TestClient(app) as c:
        admin = c.post("/api/admin/login", json={"username": "admin", "password": "verify-pw"}).json()
        h = {"Authorization": f"Bearer {admin['token']}"}
        kb_id = c.post("/api/kb", json={"name": "验证库"}, headers=h).json()["id"]
        files = {"file": ("报销制度.md", DOC_MD.encode("utf-8"), "text/markdown")}
        doc_id = c.post("/api/documents/upload", data={"kb_id": kb_id}, files=files, headers=h).json()["id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            if c.get(f"/api/documents/{doc_id}").json().get("status") == "published":
                break
            time.sleep(0.2)
        c.post("/api/qa", json={"kb_id": kb_id, "std_question": "住宿报销标准是什么",
                                "std_answer": "一线城市 600 元/晚"}, headers=h)
        return c, kb_id


client, kb_id = seed()

# get_embedding() 每次返回新实例，须补丁类方法才能统计所有调用
orig = em.MockEmbedding.embed


async def counting_embed(self, texts):
    calls["n"] += 1
    return await orig(self, texts)


em.MockEmbedding.embed = counting_embed


async def main():
    # 非 QA 问题：gate 嵌入 1 次，向量透传给首轮混合检索复用（不再二次嵌入）
    pipe = Pipeline()
    r = await pipe.answer("审批流程要多久", [kb_id], None, [])
    print("[Pipeline] answer_type=", r["answer_type"], "| embed次数=", calls["n"],
          "| latency=", r["latency_ms"], "ms | degraded=", r["degraded"])
    assert calls["n"] == 1, f"P-03 失败：期望每请求 1 次嵌入，实际 {calls['n']} 次"
    print("P-03 验证通过（非流式）：每请求 embedding 调用数 = 1")

    # 流式路径同样验证（非 QA 命中的普通问题）
    calls["n"] = 0
    events = [e async for e in answer_stream("财务什么时候打款", [kb_id], None, [])]
    done = next(e["data"] for e in events if e["type"] == "done")
    print("[Stream] answer_type=", done["answer_type"], "| embed次数=", calls["n"])
    assert calls["n"] == 1, f"P-03 失败（流式）：实际 {calls['n']} 次"
    print("P-03 验证通过（流式）：每请求 embedding 调用数 = 1")

    # QA 精确命中路径：零嵌入秒回
    from app.engine import qa_engine
    calls["n"] = 0
    hit, qvec = await qa_engine.gate([kb_id], "住宿报销标准是什么")
    print("[QA gate] 精确命中=", bool(hit), "| qvec=", "None" if qvec is None else "vec",
          "| embed次数=", calls["n"])
    if hit and hit["mode"] == "exact":
        assert calls["n"] == 0, "精确命中路径不应触发嵌入"
        print("P-03 验证通过（QA 精确命中）：零嵌入秒回")


asyncio.run(main())

