"""Rerank Provider（FR-123/ADR-005 延伸）：
- none：跳过精排（降级模式，NFR-222）
- local：本地 ONNX 模型（onnxruntime，免 torch；支持 modelscope/HF 目录自动解析）
- api：通用重排 API（Cohere /v2/rerank 形状：Cohere/Jina/SiliconFlow/vLLM/Xinference 兼容）
- cohere：api 的官方默认别名
- auto：跟随 WL2_PROFILE（local→本地路径，hybrid→路径优先否则 api，remote→api）

失败/超时一律回退 passthrough（跳过精排，标记降级由引擎层记录）。
"""
import math
from pathlib import Path
from app.config import get_settings
from app.core.metering import vendor_from_url
from app.providers.base import ChunkHit


class RerankProvider:
    name = "none"

    async def rerank(self, query: str, docs: list[ChunkHit], top_n: int = 5) -> list[ChunkHit]:
        return docs[:top_n]


class ApiRerank(RerankProvider):
    """通用 /rerank API（请求与响应形状同 Cohere v2：results[{index, relevance_score}]）。"""

    name = "api"

    def __init__(self, base_url: str, api_key: str, model: str, vendor: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model or "bge-reranker-v2-m3"
        self.vendor = vendor or vendor_from_url(base_url)

    async def rerank(self, query, docs, top_n=5):
        import httpx
        if not docs:
            return []
        # base_url 可能已含 /rerank 后缀（如智谱），也可能只有 base（如 SiliconFlow 的 /v1）
        url = self.base_url if self.base_url.endswith("/rerank") else self.base_url + "/rerank"
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                    json={"model": self.model_id, "query": query,
                          "documents": [d.text[:4000] for d in docs], "top_n": len(docs)})
                r.raise_for_status()
                results = r.json()["results"]
        except Exception:
            return docs[:top_n]
        for it in results:
            idx = int(it.get("index", -1))
            if 0 <= idx < len(docs):
                docs[idx].score = float(it.get("relevance_score", it.get("score", 0.0)))
        docs.sort(key=lambda d: (-d.score, d.chunk_id))
        return docs[:top_n]


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class LocalOnnxRerank(RerankProvider):
    """本地交叉编码器（ONNX）：bge-reranker 系列；分数经 sigmoid 归一到 0-1（对齐 BR-004 阈值口径）。"""

    name = "local"
    _sess = None
    _tok = None
    _lock = None

    def __init__(self, path: str, device: str = "", vendor: str = ""):
        self.root = Path(path)
        self.device = device
        self.vendor = vendor or "local"
        self._model_file, self._tok_dir = self._resolve()

    def _resolve(self) -> tuple[Path | None, Path | None]:
        p = self.root
        if (p / "onnx" / "model.onnx").exists():          # 目录本身即模型（含 onnx/）
            return p / "onnx" / "model.onnx", p
        for snap in sorted(p.glob("models/*--*/snapshots/*")):   # modelscope 下载结构
            if (snap / "onnx" / "model.onnx").exists():
                return snap / "onnx" / "model.onnx", snap
        if (p / "model.onnx").exists():                   # 直接指向 onnx 文件所在目录
            return p / "model.onnx", p
        return None, None

    def _load(self):
        import threading
        if LocalOnnxRerank._lock is None:
            LocalOnnxRerank._lock = threading.Lock()
        with LocalOnnxRerank._lock:   # 防止预热与请求并发双载模型（体验改造 ②）
            if self._sess is not None:
                return self._sess, self._tok
            if self._model_file is None:
                from app.core.logging_config import setup_logging
                setup_logging().error("本地重排模型未找到（%s），请检查 WL2_RERANK_LOCAL_PATH；本次检索跳过精排", self.root)
                raise FileNotFoundError(f"rerank 模型未找到 onnx/model.onnx：{self.root}")
            import onnxruntime as ort
            from transformers import AutoTokenizer
            from app.core.logging_config import setup_logging
            setup_logging().info("加载本地重排模型: %s (device=%s)", self._model_file, self.device or "auto")
            providers = ["CPUExecutionProvider"] if self.device == "cpu" \
                else (["CUDAExecutionProvider", "CPUExecutionProvider"] if self.device == "cuda"
                      else ort.get_available_providers())
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2
            self._sess = ort.InferenceSession(str(self._model_file), so, providers=providers)
            self._tok = AutoTokenizer.from_pretrained(str(self._tok_dir))
            return self._sess, self._tok
        return self._sess, self._tok

    def _score_sync(self, query: str, texts: list[str]) -> list[float]:
        sess, tok = self._load()
        enc = tok([query] * len(texts), texts, padding=True, truncation=True,
                  max_length=512, return_tensors="np")
        allowed = {i.name for i in sess.get_inputs()}
        feeds = {k: enc[k] for k in allowed if k in enc}
        logits = sess.run(None, feeds)[0]
        return [_sigmoid(float(row[0] if hasattr(row, "__len__") else row)) for row in logits]

    async def rerank(self, query, docs, top_n=5):
        import asyncio
        if not docs:
            return []
        try:
            scores = await asyncio.get_running_loop().run_in_executor(
                None, self._score_sync, query, [d.text[:4000] for d in docs])
        except Exception as e:
            from app.core.logging_config import setup_logging
            setup_logging().exception("本地重排推理失败，跳过精排（降级）: %s", e)
            return docs[:top_n]
        for d, s in zip(docs, scores):
            d.score = s
        docs.sort(key=lambda d: (-d.score, d.chunk_id))
        return docs[:top_n]


def _resolve_auto() -> str:
    """WL2_RERANK_ACTIVE=auto：跟随 WL2_PROFILE。"""
    cfg = get_settings()
    if cfg.profile == "local":
        return "local"
    if cfg.profile == "remote":
        return "api"
    return "local" if cfg.rerank_local_path else ("api" if cfg.rerank_remote_base_url else "none")


def get_reranker() -> RerankProvider:
    cfg = get_settings()
    mode = _resolve_auto() if cfg.rerank_active == "auto" else cfg.rerank_active
    if mode == "local" and cfg.rerank_local_path:
        return LocalOnnxRerank(cfg.rerank_local_path, cfg.rerank_local_device, vendor=cfg.rerank_local_vendor)
    if mode == "api" and cfg.rerank_remote_base_url:
        return ApiRerank(cfg.rerank_remote_base_url, cfg.rerank_remote_api_key,
                         cfg.rerank_remote_model, vendor=cfg.rerank_vendor)
    if mode == "cohere" and cfg.rerank_remote_api_key:
        return ApiRerank("https://api.cohere.com/v2", cfg.rerank_remote_api_key,
                         "rerank-multilingual-v3.0", vendor=cfg.rerank_vendor or "Cohere")
    return RerankProvider()   # none / 配置不完整 → 跳过精排（降级，NFR-222）
