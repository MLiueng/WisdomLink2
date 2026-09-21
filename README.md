<div align="center">

# 🧠 WisdomLink2

**企业级智能知识库平台**

RAG · QA 快速命中 · 知识图谱 · 数据清洗 · 多级溯源 · 全组件双模式

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue-3.x-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white)](https://vuejs.org)
[![LangChain](https://img.shields.io/badge/LangChain-LangGraph-1C3C3C?style=flat-square&logo=langchain&logoColor=white)](https://langchain.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)

[快速开始](#-快速开始) · [核心能力](#-核心能力) · [配置](#-配置) · [架构](#-架构) · [文档](#-文档) · [参与贡献](#-参与贡献)

</div>

---

## ✨ 特性

### 🔍 检索与问答

- **混合检索**：向量语义 + BM25 关键词 + 知识图谱三路召回，RRF 融合，本地 ONNX 重排
- **QA 快速命中**：高频问题秒回标准答案（0 生成 Token），精确匹配 + 向量相似双阈值
- **RAG 流式问答**：SSE 真流式（首 token < 2s），多轮上下文，自纠错重检索（LangGraph）
- **多级溯源**：引用 → 知识片段（含清洗 diff）→ 文档版本 → 原始文件在线预览，SHA-256 指纹校验防篡改

### 📄 文档处理

- **多格式上传**：PDF / Word / Excel / **PPT（标题/正文/表格/备注）** / Markdown / TXT / HTML / **图片（自动 OCR）**
- **数据清洗**：页眉页脚 / 水印 / 敏感信息 / 重复段落自动去除，逐条留痕，可还原重建
- **语义分片**：父子分片 + 句子边界 + 表格/代码原子化 + 类型标签（text/table/code）
- **原子发布**：generation 切换，重建期间检索不中断
- **在线预览**：全类型零外部依赖（Office 入库时自动转 Markdown，含嵌入图片提取，**不需要 LibreOffice**）
- **图片 OCR**：本地 Tesseract（Docker 内置）或远程 GLM-4V 视觉模型，`auto` 模式自动回退，中文识别 95%+

### 🕸️ 知识图谱

- **三层模型**：概念（本体 is-a）· 实体（含事件类型）· 关系（类型化谓词 + 证据链回指原文）
- **AI 抽取**：LLM Schema 约束抽取，候选态入库，人工确认后参与检索
- **可视化**：ECharts 力导向图（小图 Canvas / 大图 WebGL 自动切换）
- **治理**：健康体检（孤立实体/悬空证据/覆盖率）+ 图谱洞察（枢纽/社区聚类）

### 🛡️ 企业级

- **数据清洗可审计**：每个清洗动作留痕，清洗前后 diff 对照
- **Token 用量统计**：厂商 × 模型 × 用途 × 日纯用量（不展示价格），CSV 导出
- **全链路日志**：请求日志 + trace-id 贯穿 SSE + 滚动文件
- **降级矩阵**：LLM 故障→切本地，向量库→BM25，Rerank→跳过，Redis→内存兜底
- **问答反馈**：点赞/点踩 + 差评联动沉淀引导（一键补 QA / 跳 Wiki）

### 🔀 双模式（核心红线）

所有 AI 组件支持**本地部署**与**远程 API**两种形态，仅改配置即切换：

| 组件 | 本地 | 远程 |
|---|---|---|
| LLM | Ollama / vLLM / LMDeploy | OpenAI / DeepSeek / 通义 / GLM / SiliconFlow |
| Embedding | BGE-M3（进程内 / Xinference） | 任意 OpenAI 兼容 API |
| Rerank | 本地 ONNX（bge-reranker） | Cohere / Jina / **智谱 GLM** |
| 向量库 | FAISS（单机文件） | **Qdrant / Milvus**（集群） |
| **OCR** | **Tesseract（Docker 内置）** | **GLM-4V-flash（免费视觉模型）** |
| 图存储 | MySQL 三表 + NetworkX | Neo4j / NebulaGraph（预留） |
| 对象存储 | 本地目录 | MinIO / S3 |

---

## 🚀 快速开始

### 前置条件

- Python 3.12+ 和/或 Node.js 18+
- （可选）Docker：用于 MySQL / Redis / Qdrant / MinIO
- （可选）任一 OpenAI 兼容 LLM API Key

### 方式一：前端独立预览（零依赖，30 秒体验）

```bash
git clone https://github.com/yourname/WisdomLink2.git
cd WisdomLink2/frontend
npm install
npm run dev
# 打开 http://localhost:5173 —— Mock 模式，内置演示数据
```

### 方式二：完整运行

```bash
# 1. 基础设施（可选：不启动则后端自动降级 SQLite + 内存缓存 + FAISS）
docker compose up -d

# 2. 后端
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows（Linux: source .venv/bin/activate）
pip install -r requirements.txt
copy .env.example .env            # 填入你的模型 API Key
python main.py                    # http://localhost:8000（/docs 查看 Swagger）

# 3. 前端
cd ../frontend
# 编辑 .env.development → VITE_USE_MOCK=false
npm install && npm run dev        # http://localhost:5173
```

### 方式三：纯本地离线（零外部 API，数据不出内网）

```ini
# .env
WL2_LLM_ACTIVE=local
WL2_LLM_LOCAL_BASE_URL=http://localhost:11434/v1     # Ollama
WL2_VECTOR_DRIVER=faiss
# 不配置 WL2_DB_URL / WL2_REDIS_URL → 自动使用 SQLite + 内存缓存
```

### 验证运行

```bash
curl http://localhost:8000/healthz
# {"status":"up"}

curl http://localhost:8000/readyz
# {"status":"up","components":{"api":"up","db":"up","redis":"degraded(memory)","vector":"faiss"}}
```

---

## 📦 核心能力

| 能力 | 说明 |
|---|---|
| 多知识库+文件夹 | 库=权限单元，库内多级文件夹分类（≤5 级），检索范围可细化到文件夹 |
| 文档全生命周期 | 上传→SHA-256 判重→解析→清洗→分片→索引→发布；软删除回收站 30 天自动清理 |
| 混合检索 | 三路召回 + RRF + ONNX 重排 + 相关性下限过滤 + 相邻分片补充 |
| QA 快速命中 | 归一化精确 + 向量相似双阈值，命中优先级：QA 快速命中 → RAG |
| 流式问答 | SSE 真流式，多轮指代消解，拒答（无依据不编造），冲突提示（双引用+最新生效） |
| 多级溯源 | 四级下抽屉 + 指纹校验 + 原件在线预览（全类型零依赖） |
| Wiki | Markdown 编辑 + 页面树（复用知识库文件夹结构），页面编辑/上下线/删除 + 树内文件夹管理（悬停/右键菜单），发布走入库流水线（编辑=原地新版本），宽屏内联/窄屏 Drawer |
| 知识图谱 | 三层模型 + LLM 抽取 + 批量确认 + 力导向图 + 体检/洞察 |
| Token 用量 | 纯用量口径（不展示价格），厂商自动识别 + 手动覆盖 |
| 运行时配置 | Top-K / 重排数 / 相关性下限即时生效（不用重启），范围校验 + 审计快照 |
| 会话管理 | 置顶 / 搜索 / 反馈 / 取消流 |
| 降级矩阵 | 任一组件故障自动降级，服务持续可用 |

---

## ⚙️ 配置

所有配置经 `backend/.env` 注入（复制 `.env.example`）：

<details>
<summary><b>点击展开完整配置项</b></summary>

```ini
# === 整体姿态 ===
WL2_PROFILE=hybrid                  # local | remote | hybrid（ACTIVE=auto 时跟随）

# === LLM（OpenAI 兼容协议）===
WL2_LLM_ACTIVE=remote               # remote | local | mock | auto
WL2_LLM_VENDOR=DeepSeek             # 厂商标签（空=URL 自动识别）
WL2_LLM_REMOTE_BASE_URL=https://api.deepseek.com/v1
WL2_LLM_REMOTE_API_KEY=sk-xxx
WL2_LLM_REMOTE_MODEL=deepseek-chat
WL2_LLM_LOCAL_BASE_URL=http://localhost:11434/v1
WL2_LLM_LOCAL_MODEL=qwen2.5:7b

# === Embedding ===
WL2_EMB_ACTIVE=remote
WL2_EMB_VENDOR=bigmodel
WL2_EMB_REMOTE_BASE_URL=https://open.bigmodel.cn/api/paas/v4
WL2_EMB_REMOTE_MODEL=embedding-3

# === Rerank（可选）===
WL2_RERANK_ACTIVE=api               # none | local | api | auto
WL2_RERANK_REMOTE_BASE_URL=https://open.bigmodel.cn/api/paas/v4/rerank
WL2_RERANK_REMOTE_MODEL=rerank      # 智谱模型名
# 或本地模式：
# WL2_RERANK_ACTIVE=local
# WL2_RERANK_LOCAL_PATH=./models/bge-reranker-base

# === 向量库 ===
WL2_VECTOR_DRIVER=qdrant            # qdrant | milvus | faiss
WL2_QDRANT_URL=http://localhost:6333
WL2_MILVUS_URI=http://localhost:19530

# === OCR（图片文字提取）===
WL2_OCR_ACTIVE=auto                 # none | local | api | auto
WL2_OCR_API_BASE_URL=https://open.bigmodel.cn/api/paas/v4
WL2_OCR_API_MODEL=glm-4v-flash     # 智谱免费视觉模型

# === 检索质量 ===
WL2_QA_SIM_THRESHOLD=0.92           # QA 相似命中阈值
WL2_QA_MARGIN=0.02                  # 防误命中的分差下限
WL2_RELEVANCE_FLOOR=0.2             # 重排后相关性下限（低于不返回）

# === 数据库/缓存（不配置=自动降级）===
# WL2_DB_URL=mysql+pymysql://user:pass@localhost:3306/wl2
# WL2_REDIS_URL=redis://localhost:6379/0

# === 服务 ===
WL2_SECRET=change-me-32-chars-min
WL2_ADMIN_PW=admin123
WL2_PORT=8000
```

</details>

---

## 🏗️ 架构

```
Vue 3 SPA (15 页面)
    │ HTTP + SSE
    ▼
FastAPI 模块化单体（全异步）
    ├─ API 路由层（6 个 Router · 71 个接口）
    ├─ 引擎层（清洗 / 语义分片 / 入库 / 三路检索 / QA命中 / LangGraph 流式编排）
    ├─ Provider 适配层（LLM / Embed / Rerank / 向量库 / 图存储 / 对象存储 / 解析 / OCR）
    └─ 核心层（配置 / 缓存 / BR规则 / JWT / 计量 / 日志）
    │
    ├─ MySQL（17 张表，含图谱三表）
    ├─ Redis（QA字典 / 队列，可降级内存）
    ├─ Qdrant / Milvus / FAISS（chunk 集合 + QA 集合物理隔离）
    ├─ 对象存储（本地 / MinIO-S3，原子写入）
    └─ LLM / 视觉模型 API（OpenAI 兼容，本地或远程）
```

<details>
<summary><b>目录结构</b></summary>

```
backend/
  main.py                  # 启动入口
  app/
    config.py              # 全部配置
    models.py              # 17 张表
    core/                  # cache / rules / security / metering / logging
    providers/             # llm / embedding / rerank / vector_store / kg_store / ...
    engine/                # clean / semantic_chunker / ingest / retrieval / qa_engine / graph
    api/routers/           # kb / documents / qa / chat / kg / admin
  tests/                   # 规则 8 + 集成 20 + 真实冒烟
frontend/
  src/
    styles/tokens.css      # 设计 Token
    views/                 # 15 个页面
    components/            # CitationDrawer / StatusBadge / ...
```

</details>

---

## 🧪 测试

```bash
cd backend
.venv\Scripts\python -m pytest tests/test_rules.py tests/test_integration.py -q
# 28 passed（规则 8 + 集成 20，TC-701~720）

.venv\Scripts\python tests/live_smoke.py
# 真实厂商冒烟（DeepSeek 生成 / 智谱向量 / 本地重排）
```

---

## 📖 文档

| 文档 | 说明 |
|---|---|
| [需求文档](docs/需求文档.md) | 已交付业务能力（代码现状反向整理） |
| [需求规格说明书](docs/需求规格说明书.md) | 正向需求基线（MoSCoW + BR 业务规则） |
| [需求分析设计文档](docs/需求分析设计文档.md) | 架构 / 模块 / 时序 / 数据模型 / 检索策略 |
| [系统设计文档](docs/系统设计文档.md) | 架构 / 模块 / 数据流 / 安全 / 降级 |
| [详细设计文档](docs/详细设计文档.md) | 接口 / 数据表 / 核心算法 / SSE 协议 |
| [前端系统设计文档](docs/前端系统设计文档.md) | 设计 Token / 组件规范 / 页面结构 |
| [测试用例文档](docs/测试用例文档.md) | TC-701…720 覆盖矩阵 + 分层测试策略 |
| [第二版扩展功能文档](docs/第二版扩展功能文档.md) | 认证 RBAC / 网关 / 开放 API |

---

## 🗺️ 路线图

- [x] V1：核心知识库（检索/问答/溯源/图谱/QA/清洗）
- [x] 真流式输出 + 冷启动预热
- [x] Office 在线预览（Markdown 转换 + 嵌入图片，零外部依赖）
- [x] 运行时检索参数配置
- [x] Milvus 向量库适配（分布式分片/副本）
- [x] 智谱 Rerank API 集成
- [x] PPT 解析（标题/正文/表格/备注）
- [x] 图片 OCR（Tesseract 本地 / GLM-4V 远程 / auto 回退）
- [x] Docker 部署（Tesseract 内置于后端镜像）
- [ ] V2：认证 RBAC / API 网关 / 开放 API / 多租户
- [ ] 性能压测（100 万向量）

---

## 🤝 参与贡献

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/amazing-feature`
3. 提交更改：`git commit -m 'feat: add amazing feature'`
4. 推送分支：`git push origin feature/amazing-feature`
5. 提交 Pull Request

---

## 📄 License

本项目采用 [MIT License](LICENSE) 开源。

---

<div align="center">

**如果这个项目对你有帮助，请给一个 ⭐ Star！**

</div>
