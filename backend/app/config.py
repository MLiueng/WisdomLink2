"""应用配置：全部经环境变量注入（用户自行配置 .env），默认值仅保证可启动。"""
import json
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    env: str = "dev"
    secret: str = "wl2-dev-secret"
    admin_pw: str = "admin123"
    token_ttl_min: int = 120          # 管理员 JWT 有效期（分钟，S-01 加固）
    upload_max_mb: int = 100          # 单文件上传上限（MB，S-03）
    cors_origins: str = "http://localhost:5173"

    db_url: str = "sqlite:///./data/wl2.db"
    redis_url: str = "redis://localhost:6379/0"

    obj_driver: str = "local"
    obj_local_dir: str = "./data/objects"
    s3_endpoint: str = ""
    s3_key: str = ""
    s3_secret: str = ""
    s3_bucket: str = "wl2"

    profile: str = "hybrid"

    llm_remote_base_url: str = ""
    llm_remote_api_key: str = ""
    llm_remote_model: str = "gpt-4o-mini"
    llm_local_base_url: str = ""
    llm_local_api_key: str = ""
    llm_local_model: str = "qwen2.5:7b"
    llm_active: str = "mock"  # remote | local | mock | auto
    llm_vendor: str = ""        # 远程 LLM 厂商标签（空=按 URL 自动识别；自部署可自命名）
    llm_local_vendor: str = ""  # 本地/自部署 LLM 厂商标签（空=自动，localhost → local）

    emb_remote_base_url: str = ""
    emb_remote_api_key: str = ""
    emb_remote_model: str = "text-embedding-3-small"
    emb_active: str = "mock"  # remote | mock | auto
    emb_vendor: str = ""        # 远程 Embedding 厂商标签（空=自动）
    emb_local_vendor: str = ""  # 本地 Embedding 厂商标签（mock 模式自动为 mock）

    rerank_active: str = "none"  # none | local | api | cohere | auto
    rerank_local_path: str = ""          # 本地模型目录（含 onnx/model.onnx 或 modelscope 快照）
    rerank_local_device: str = ""        # cpu | cuda | ""(自动)
    rerank_remote_base_url: str = ""     # api 模式：Cohere/Jina/SiliconFlow/vLLM/Xinference 的 /rerank 端点
    rerank_remote_model: str = "bge-reranker-v2-m3"
    rerank_remote_api_key: str = ""
    rerank_vendor: str = ""              # api/cohere 模式厂商标签（空=自动）
    rerank_local_vendor: str = ""        # 本地 Rerank 厂商标签（空=local）

    vector_driver: str = "faiss"  # qdrant | faiss
    qdrant_url: str = "http://localhost:6333"
    milvus_uri: str = "http://localhost:19530"     # Milvus standalone 或集群地址
    milvus_token: str = ""                        # 认证 token（可选）
    milvus_db: str = "default"                    # 数据库名
    faiss_dir: str = "./data/faiss"

    relevance_floor: float = 0.2   # 重排后相关性下限（sigmoid 口径），低于该值的引用不返回（问题 2）
    log_level: str = "INFO"
    log_dir: str = "./logs"

    qa_sim_threshold: float = 0.92
    qa_margin: float = 0.02
    qa_review_days: int = 90

    # 语义答案缓存（P-04）：L2 向量近邻判定复用 BR-011 双阈值口径
    ans_cache_sim_threshold: float = 0.92
    ans_cache_margin: float = 0.02

    # 实体链接向量化（B-07）：查询向量与实体向量近邻阈值（灰度 runtime_kg_vector_link）
    kg_link_sim_threshold: float = 0.85

    price_book: str = "{}"
    currency: str = "CNY"
    daily_token_limit: int = 20_000_000

    # 限流（S-06）：固定窗口每分钟上限；<=0 视为不限流
    rate_login_per_min: int = 5      # 登录尝试（防暴力破解）
    rate_chat_per_min: int = 60      # 免登录问答（防刷量）

    soffice_path: str = ""

    # OCR（图片文字提取）
    ocr_active: str = "auto"                  # none | local | api | auto（local→api 逐级尝试）
    ocr_api_base_url: str = ""                 # api 模式：OpenAI 兼容视觉模型端点（如智谱 https://open.bigmodel.cn/api/paas/v4）
    ocr_api_key: str = ""
    ocr_api_model: str = "glm-4v-flash"        # 免费视觉模型（glm-4v-plus 亦可）

    # 联网检索（本地召回不足时的外部补充；灰度总开关在 sys_config runtime_web_search）
    web_search_active: str = "none"            # none | serper | bing | duckduckgo | mock
    web_search_api_key: str = ""
    web_search_base_url: str = ""              # 自定义/自建搜索端点（兼容 serper 协议时填）
    web_search_top_n: int = 5                  # 单次联网检索返回片段数上限
    web_search_timeout: float = 5.0            # 超时上限（硬约束：外部调用 2-5s）

    model_config = {"env_prefix": "WL2_", "env_file": ".env", "extra": "ignore"}

    @property
    def prices(self) -> dict:
        try:
            return json.loads(self.price_book or "{}")
        except json.JSONDecodeError:
            return {}

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
