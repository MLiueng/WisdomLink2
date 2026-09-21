"""LLM Provider：统一 OpenAI 兼容协议（ADR-005），本地/远程/ mock 三态。"""
import json
from collections.abc import AsyncIterator
from app.config import get_settings
from app.core.metering import vendor_from_url
from app.providers.base import Usage, ProviderError


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 1.6))  # 中文粗估：约 1.6 字符/token


class LLMProvider:
    name = "base"
    model_id = ""

    async def chat(self, messages: list[dict], temperature: float = 0.3,
                   max_tokens: int | None = None) -> tuple[str, Usage]:
        raise NotImplementedError

    async def chat_stream(self, messages: list[dict], temperature: float = 0.3) -> AsyncIterator[tuple[str, Usage | None]]:
        raise NotImplementedError
        yield "", None  # pragma: no cover


class OpenAICompatLLM(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model: str, vendor: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model
        self.name = "openai_compat"
        self.vendor = vendor or vendor_from_url(base_url)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def chat(self, messages: list[dict], temperature: float = 0.3,
                   max_tokens: int | None = None) -> tuple[str, Usage]:
        import httpx
        cfg = get_settings()
        body = {"model": self.model_id, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                r = await cli.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            raise ProviderError(f"LLM 调用失败: {e}") from e
        usage = data.get("usage") or {}
        # M5：Prompt Caching 命中统计（OpenAI 兼容口径：prompt_tokens_details.cached_tokens）
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                  or usage.get("cached_tokens") or 0)
        return (data["choices"][0]["message"]["content"] or "",
                Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                      cached_tokens=int(cached or 0)))

    async def chat_stream(self, messages: list[dict], temperature: float = 0.3) -> AsyncIterator[tuple[str, Usage | None]]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=180) as cli:
                async with cli.stream("POST", f"{self.base_url}/chat/completions", headers=self._headers(), json={
                        "model": self.model_id, "messages": messages, "temperature": temperature,
                        "stream": True, "stream_options": {"include_usage": True}}) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            chunk = json.loads(body)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            u = chunk["usage"]
                            yield "", Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                        if delta:
                            yield delta, None
        except Exception as e:
            raise ProviderError(f"LLM 流式调用失败: {e}") from e


class MockLLM(LLMProvider):
    """演示用回显模型（仅联调 UI，勿用于生产）。"""
    name, model_id, vendor = "mock", "mock-chat", "mock"

    async def chat(self, messages: list[dict], temperature: float = 0.3,
                   max_tokens: int | None = None) -> tuple[str, Usage]:
        prompt = messages[-1]["content"] if messages else ""
        text = f"[mock] 已基于知识库资料生成回答要点：{prompt[:80]}……（配置 WL2_LLM_ACTIVE 切换真实模型）"
        return text, Usage(_estimate_tokens(prompt), _estimate_tokens(text), True)

    async def chat_stream(self, messages: list[dict], temperature: float = 0.3) -> AsyncIterator[tuple[str, Usage | None]]:
        import asyncio
        text, usage = await self.chat(messages, temperature)
        for i in range(0, len(text), 6):
            yield text[i:i + 6], None
            await asyncio.sleep(0.03)
        yield "", usage


def _resolve_active(active: str, kind: str) -> str:
    """WL2_*_ACTIVE=auto：跟随 WL2_PROFILE（local→本地，remote→远程，hybrid→远程已配置则远程否则本地）。"""
    if active != "auto":
        return active
    cfg = get_settings()
    if kind == "llm":
        remote_ok = bool(cfg.llm_remote_base_url)
        local_ok = bool(cfg.llm_local_base_url)
    else:
        remote_ok = bool(cfg.emb_remote_base_url)
        local_ok = True  # embedding 本地 mock/进程内兜底始终可用
    if cfg.profile == "local":
        return "local" if local_ok else "remote"
    if cfg.profile == "remote":
        return "remote" if remote_ok else "local"
    return "remote" if remote_ok else "local"


def get_llm() -> LLMProvider:
    cfg = get_settings()
    mode = _resolve_active(cfg.llm_active, "llm")
    if mode == "remote" and cfg.llm_remote_base_url:
        return OpenAICompatLLM(cfg.llm_remote_base_url, cfg.llm_remote_api_key,
                               cfg.llm_remote_model, vendor=cfg.llm_vendor)
    if mode == "local" and cfg.llm_local_base_url:
        return OpenAICompatLLM(cfg.llm_local_base_url, cfg.llm_local_api_key,
                               cfg.llm_local_model, vendor=cfg.llm_local_vendor)
    return MockLLM()
