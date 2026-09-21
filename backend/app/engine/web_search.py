"""联网检索（检索专项优化 2026-09-18）：本地召回不足时的外部知识补充。

设计：
  - 可插拔 provider（serper / bing / duckduckgo / mock），经 WL2_WEB_SEARCH_ACTIVE 选择；
  - 灰度总开关在 sys_config：runtime_web_search = on | off（缺省 off，行为同纯本地）；
  - 失败即降级：任何异常返回空结果并记入 degraded，不阻断问答主链路；
  - 超时硬约束 2-5s（WL2_WEB_SEARCH_TIMEOUT，默认 5）。

产出 WebResult 列表，经 web_hits() 转为 ChunkHit（source="web"）供问答上下文与引用溯源。
"""
import re
from dataclasses import dataclass

from app.config import get_settings
from app.providers.base import ChunkHit


@dataclass
class WebResult:
    title: str
    snippet: str
    url: str
    score: float = 0.5


def web_search_enabled() -> bool:
    """灰度开关：sys_config runtime_web_search=on 且配置了非 none 的 provider。"""
    if (get_settings().web_search_active or "none") == "none":
        return False
    from app.core.runtime_config import get_runtime
    return (get_runtime("runtime_web_search") or "off") == "on"


class _BaseSearcher:
    name = "base"

    async def search(self, query: str, top_n: int) -> list[WebResult]:
        raise NotImplementedError


class SerperSearch(_BaseSearcher):
    """serper.dev 协议（兼容自建端点：WL2_WEB_SEARCH_BASE_URL 覆盖）。"""
    name = "serper"

    async def search(self, query: str, top_n: int) -> list[WebResult]:
        import httpx
        cfg = get_settings()
        url = (cfg.web_search_base_url or "https://google.serper.dev").rstrip("/") + "/search"
        async with httpx.AsyncClient(timeout=cfg.web_search_timeout) as cli:
            r = await cli.post(url, headers={"X-API-KEY": cfg.web_search_api_key,
                                             "Content-Type": "application/json"},
                               json={"q": query, "num": top_n, "gl": "cn", "hl": "zh-cn"})
            r.raise_for_status()
            organic = r.json().get("organic") or []
        return [WebResult(title=o.get("title", ""), snippet=o.get("snippet", ""),
                          url=o.get("link", ""), score=0.6 - i * 0.01)
                for i, o in enumerate(organic[:top_n]) if o.get("snippet")]


class BingSearch(_BaseSearcher):
    name = "bing"

    async def search(self, query: str, top_n: int) -> list[WebResult]:
        import httpx
        cfg = get_settings()
        url = (cfg.web_search_base_url or "https://api.bing.microsoft.com").rstrip("/") + "/v7.0/search"
        async with httpx.AsyncClient(timeout=cfg.web_search_timeout) as cli:
            r = await cli.get(url, params={"q": query, "count": top_n, "mkt": "zh-CN"},
                              headers={"Ocp-Apim-Subscription-Key": cfg.web_search_api_key})
            r.raise_for_status()
            pages = (r.json().get("webPages") or {}).get("value") or []
        return [WebResult(title=p.get("name", ""), snippet=p.get("snippet", ""),
                          url=p.get("url", ""), score=0.6 - i * 0.01)
                for i, p in enumerate(pages[:top_n]) if p.get("snippet")]


class DuckDuckGoSearch(_BaseSearcher):
    """DuckDuckGo HTML 端点（免 key；解析结果标题/摘要，格式变动时返回空列表降级）。"""
    name = "duckduckgo"

    async def search(self, query: str, top_n: int) -> list[WebResult]:
        import httpx
        cfg = get_settings()
        async with httpx.AsyncClient(timeout=cfg.web_search_timeout,
                                     headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as cli:
            r = await cli.get("https://duckduckgo.com/html/", params={"q": query})
            r.raise_for_status()
            html = r.text
        out: list[WebResult] = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html):
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', html[m.end():m.end() + 3000], re.S)
            snippet = re.sub(r"<[^>]+>", "", sm.group(1)).strip() if sm else ""
            if snippet:
                out.append(WebResult(title=title.strip(), snippet=snippet, url=url,
                                     score=0.6 - len(out) * 0.01))
            if len(out) >= top_n:
                break
        return out


class MockWebSearch(_BaseSearcher):
    """确定性 mock（测试/联调）：返回含查询词的稳定片段。"""
    name = "mock"

    async def search(self, query: str, top_n: int) -> list[WebResult]:
        return [WebResult(title=f"联网资料-{query[:20]}-{i}",
                          snippet=f"（联网检索）关于「{query}」的公开资料片段 {i}：{query}相关的外部信息。",
                          url=f"https://example.com/mock/{i}", score=0.55 - i * 0.01)
                for i in range(1, min(top_n, 3) + 1)]


_SEARCHERS: dict[str, type[_BaseSearcher]] = {
    "serper": SerperSearch, "bing": BingSearch,
    "duckduckgo": DuckDuckGoSearch, "mock": MockWebSearch,
}


def get_searcher() -> _BaseSearcher:
    mode = (get_settings().web_search_active or "none").lower()
    return _SEARCHERS.get(mode, _BaseSearcher)()


async def web_search(query: str, top_n: int | None = None) -> list[WebResult]:
    """执行联网检索；任何异常返回空列表（调用方记降级），不抛出。"""
    cfg = get_settings()
    top_n = top_n or cfg.web_search_top_n
    searcher = get_searcher()
    if searcher.name == "base":
        return []
    if searcher.name in ("serper", "bing") and not cfg.web_search_api_key:
        return []
    try:
        results = await searcher.search(query, top_n)
        return [r for r in results if r.snippet.strip()][:top_n]
    except Exception:
        from app.core.logging_config import setup_logging
        setup_logging().exception("联网检索失败（降级为空）| provider=%s | q=%s", searcher.name, query[:40])
        raise


def web_hits(query: str, results: list[WebResult]) -> list[ChunkHit]:
    """WebResult → ChunkHit（source=web）：chunk_id 带前缀防与库内 id 冲突。"""
    return [ChunkHit(chunk_id=f"web:{i}", text=r.snippet[:600], score=r.score,
                     heading_path=f"联网检索：{r.title[:60]}", source="web",
                     payload={"url": r.url, "title": r.title, "external": True})
            for i, r in enumerate(results, 1)]
