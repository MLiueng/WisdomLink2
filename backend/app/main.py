"""WisdomLink2 应用装配（app 对象定义处）。

启动入口（三者等价，均含 main 方法）：
  python main.py            # backend/ 下，推荐
  python -m app.main        # backend/ 下
  python app/main.py        # 任意目录（脚本模式自动校正 import 路径与工作目录）
启动逻辑统一定义在 backend/main.py 的 main()，本文件不重复实现。
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if __package__ in (None, ""):   # 脚本模式（python app/main.py）：恢复 backend 为 import 根
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.chdir(Path(__file__).resolve().parents[1])   # 工作目录校正到 backend/（.env/data/logs 相对路径一致）

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pathlib import Path

from app.config import get_settings
from app.core.logging_config import setup_logging, new_trace_id, Timer
from app.db import Base, engine
from app.api.routers import admin, chat, documents, kb, kg, qa

log = setup_logging(get_settings().log_level, get_settings().log_dir)


def _ensure_columns() -> None:
    """既有库轻量补列（SQLite）：create_all 不给已存在表加列，新增字段需手动 ALTER。

    仅补本轮迭代新增列（M5 model_usage.cached_tokens、A-07 kg_edge.evidence_clean_hash）；
    幂等，重复执行安全。
    MySQL 需 DBA 走正式迁移脚本，此处仅覆盖 SQLite 开发形态。
    """
    if not get_settings().db_url.startswith("sqlite"):
        return
    from sqlalchemy import text
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(model_usage)"))}
        if "cached_tokens" not in cols:
            conn.execute(text("ALTER TABLE model_usage ADD COLUMN cached_tokens INTEGER DEFAULT 0"))
            conn.commit()
            log.info("模型库迁移：model_usage 补列 cached_tokens（M5 缓存命中计量）")
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(kg_edge)"))}
        if "evidence_clean_hash" not in cols:
            conn.execute(text("ALTER TABLE kg_edge ADD COLUMN evidence_clean_hash VARCHAR(64) DEFAULT NULL"))
            conn.commit()
            log.info("模型库迁移：kg_edge 补列 evidence_clean_hash（A-07 证据指纹化）")


def _ensure_indexes() -> None:
    """P-08 既有库补热路径索引（SQLite）：create_all 不给已存在表补索引。

    幂等（CREATE INDEX IF NOT EXISTS），重复执行安全；
    MySQL 需 DBA 迁移脚本同步（与 _ensure_columns 同约定）。
    """
    if not get_settings().db_url.startswith("sqlite"):
        return
    from sqlalchemy import text
    ddl = [
        "CREATE INDEX IF NOT EXISTS ix_chunk_kb_role_ver ON chunk_meta(kb_id, role, doc_version_id)",
        "CREATE INDEX IF NOT EXISTS ix_doc_version_is_current ON doc_version(is_current)",
        "CREATE INDEX IF NOT EXISTS ix_document_status ON document(status)",
        "CREATE INDEX IF NOT EXISTS ix_kgnode_kb_status ON kg_node(kb_id, status)",
    ]
    with engine.connect() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_indexes()
    cfg = get_settings()
    import sys
    log.info("启动 | python=%s | profile=%s | llm=%s@%s | emb=%s | rerank=%s path=%s | vector=%s | db=%s",
             sys.executable, cfg.profile, cfg.llm_active, cfg.llm_remote_base_url or cfg.llm_local_base_url or "-",
             cfg.emb_active, cfg.rerank_active, cfg.rerank_local_path or "-", cfg.vector_driver,
             cfg.db_url.split("://")[0])
    if cfg.rerank_active == "local" and not cfg.rerank_local_path:
        log.warning("WL2_RERANK_ACTIVE=local 但未配置 WL2_RERANK_LOCAL_PATH，重排将跳过（降级）")
    log.info("提示：请确认使用 .venv 的 python 启动（全局环境缺少 onnxruntime 等会导致本地重排静默失败）")
    from app.engine.cleanup import purge_expired, start_daemon
    purge_expired(30)
    start_daemon(30)
    # A-03：启动恢复——把崩溃前卡在中间态的文档重入库（幂等，互斥防并发重复）
    from app.engine.ingest import recover_stuck_documents
    n = await recover_stuck_documents()
    if n:
        log.info("启动恢复：%d 个中间态文档已重入库任务", n)
    # P-10：计量/审计批处理刷盘线程启动（写事务出热路径；关停时排空残留）
    from app.core.metering import start_flusher, stop_flusher
    start_flusher()
    yield
    stop_flusher()


TAGS_METADATA = [
    {"name": "kb", "description": "知识库与文件夹管理。知识库是唯一的权限与运营单元（FR-102）；库内多级文件夹仅分类、不参与权限（V2 以库为维度授权）。"},
    {"name": "documents", "description": "文档全生命周期：上传（SHA-256 判重 BR-002）→ 解析 → 清洗（留痕 BR-009）→ 分片（BR-003）→ 索引 → 发布；状态机 BR-008；多级溯源与原件预览（FR-118、BR-010）。"},
    {"name": "qa", "description": "QA 问答对管理与快速命中（FR-131）：归一化精确 → 向量相似双阈值（BR-011）；变更后字典/向量 ≤1 分钟生效（NFR-264）。"},
    {"name": "chat", "description": "会话与 SSE 流式问答（FR-107/114）。命中优先级：QA → 语义缓存 → RAG；LangGraph 编排（意图→改写→检索→充分性→生成→引用校验）。"},
    {"name": "kg", "description": "知识图谱：概念（本体）/实体/关系三层内容（FR-121）、抽取候选校对、关联检索预览（FR-117）。"},
    {"name": "admin", "description": "管理面（内置管理员守门，NFR-219）：登录、Token 用量（纯用量口径，2026-09-12 裁决）、清洗规则、审计、健康检查、检索评测。"},
]

app = FastAPI(
    title="WisdomLink2 API",
    version="1.2.0",
    description=(
        "WisdomLink2 企业级智能知识库平台（V1）。\n\n"
        "**核心能力**：多知识库 + 文件夹分类 · 多格式解析 · 数据清洗（留痕可还原）· 父子分片 · "
        "混合检索（向量+BM25+图谱）· 本地重排 · QA 快速命中 · RAG 流式问答 · 多级溯源 · Token 用量统计。\n\n"
        "**双模式红线**：LLM / Embedding / Rerank / 向量库 / 图存储 / 对象存储 / 解析 / 预览 全部支持"
        "本地部署与远程 API 两种形态，仅经 `.env` 配置切换（OpenAI 兼容协议）。\n\n"
        "**认证边界（V1）**：部署于可信内网；管理面（/api/admin/* 及写操作）需 JWT（`POST /api/admin/login`）；"
        "问答/检索入口免登录。认证/权限/网关/开放 API 属 V2（WL2-DOC-003）。\n\n"
        "**接口约定**：除健康检查外均以 `/api` 前缀；错误统一返回 {detail: 错误信息}；"
        "问答流式接口为 SSE（`text/event-stream`），事件协议见 `POST /api/chat/stream` 说明。"
    ),
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_list,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_logging(request, call_next):
    trace = new_trace_id()
    timer = Timer()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("请求异常 | %s %s | trace=%s", request.method, request.url.path, trace)
        raise
    response.headers["X-Trace-Id"] = trace
    log.info("%s %s -> %s | %dms | trace=%s", request.method, request.url.path,
             response.status_code, timer.ms(), trace)
    return response

app.include_router(kb.router)
app.include_router(documents.router)
app.include_router(qa.router)
app.include_router(chat.router)
app.include_router(kg.router)
app.include_router(admin.login_router)   # /login 豁免整体鉴权（S-02）
app.include_router(admin.router)


@app.get("/healthz")
def healthz():
    return {"status": "up"}


@app.get("/readyz")
def readyz():
    # 探针分离（审计 M5）：healthz=存活（零依赖，上方）；readyz=就绪（组件深探）。
    # 同步函数走线程池执行，DB/Redis/向量库慢时不阻塞事件循环。
    return admin.health()


web_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if web_dist.exists():
    class _SPAStaticFiles(StaticFiles):
        """SPA 回退：前端为 history 路由，非静态资源的深链（/kb、/kb/1…）
        直接访问或刷新时返回 index.html，交由前端路由处理（避免 404 白屏）。
        注：新版 Starlette 找不到文件时抛 HTTPException(404) 而非返回响应，须捕获。"""

        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as e:
                if e.status_code != 404:
                    raise
                # API/系统路径不回退（保留真实 404），其余深链交给前端路由
                if path.startswith(("api/", "docs", "openapi", "healthz", "readyz")):
                    raise
                return await super().get_response("index.html", scope)

    app.mount("/", _SPAStaticFiles(directory=str(web_dist), html=True), name="web")


if __name__ == "__main__":     # 与 backend/main.py 等价的启动入口（逻辑不重复，直接转发）
    from main import main as _main
    _main()
