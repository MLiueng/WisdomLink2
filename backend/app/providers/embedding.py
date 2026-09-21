"""Embedding Provider：远程 OpenAI 兼容 / mock 哈希向量（确定性、仅演示联调）。"""
import hashlib
import math
from app.config import get_settings
from app.core.metering import vendor_from_url
from app.providers.base import ProviderError

EMBED_BATCH = 32  # B-03：单批上限（多数远程服务 100 以内安全）


class EmbeddingProvider:
    name = "base"
    model_id = ""
    dim = 0
    vendor = "unknown"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class RemoteEmbedding(EmbeddingProvider):
    def __init__(self, base_url: str, api_key: str, model: str, dim: int = 1024, vendor: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model
        self.dim = dim
        self.name = "openai_compat"
        self.vendor = vendor or vendor_from_url(base_url)

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as cli:
                r = await cli.post(f"{self.base_url}/embeddings",
                                   headers={"Authorization": f"Bearer {self.api_key}"},
                                   json={"model": self.model_id, "input": texts})
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                vecs = [d["embedding"] for d in data]
        except Exception as e:
            raise ProviderError(f"Embedding 调用失败: {e}") from e
        if vecs and vecs[0]:
            self.dim = len(vecs[0])
        return vecs

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """B-03：分批发送（每批 32 条），避免大文档单请求超限/超时且整文档失败；
        单批失败重试一次（批级容错），再失败才上抛。"""
        import asyncio
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            try:
                out.extend(await self._embed_batch(batch))
            except ProviderError:
                await asyncio.sleep(0.5)
                out.extend(await self._embed_batch(batch))
        return out


class MockEmbedding(EmbeddingProvider):
    """确定性哈希向量（词级 256 维，仅演示检索链路；上线前切换真实模型）。"""
    name, model_id, dim, vendor = "mock", "mock-embed", 256, "mock"

    @staticmethod
    def _vec(text: str) -> list[float]:
        v = [0.0] * 256
        import jieba
        for tok in jieba.lcut(text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % 256] += 1.0 + (h >> 8) % 100 / 1000
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # P-02：jieba 分词为同步 CPU 段（首请求触发词典加载约 1s），卸载线程池
        import asyncio
        return await asyncio.to_thread(lambda: [self._vec(t or " ") for t in texts])


def get_embedding() -> EmbeddingProvider:
    from app.providers.llm import _resolve_active
    mode = _resolve_active(get_settings().emb_active, "embedding")
    cfg = get_settings()
    if mode == "remote" and cfg.emb_remote_base_url:
        return RemoteEmbedding(cfg.emb_remote_base_url, cfg.emb_remote_api_key,
                               cfg.emb_remote_model, vendor=cfg.emb_vendor)
    return MockEmbedding()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)
