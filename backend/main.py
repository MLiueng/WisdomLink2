"""WisdomLink2 启动入口（与 app 包同级）。

用法：
  python main.py            # 推荐：任意目录执行均可（自动校正工作目录到 backend/）
  python -m uvicorn app.main:app --reload   # 开发热重载（需在 backend 目录、.venv 下）

工作目录会被强制校正为 backend/，确保 .env、data/、logs/、models/ 等相对路径
在任何启动方式下都一致——避免"从子目录启动导致配置丢失"的问题。
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)                      # 关键：统一工作目录（根治 cwd 错位类问题）
if str(BASE_DIR) not in sys.path:       # 支持从任意目录以 python backend/main.py 启动
    sys.path.insert(0, str(BASE_DIR))

from app.main import app, log           # noqa: E402  （导入即完成日志/配置初始化）
from app.config import get_settings     # noqa: E402


def main() -> None:
    cfg = get_settings()
    import uvicorn
    reload_on = os.getenv("WL2_RELOAD") == "1"   # 默认关闭：热重载子进程可能落到全局 Python
    if reload_on:
        log.warning("已开启热重载（WL2_RELOAD=1）：请确认以 .venv 的 python 启动，"
                    "否则本地重排/解析等依赖不可用")
    port = int(os.getenv("WL2_PORT", "8000"))
    log.info("入口 main.py | cwd=%s | 端口=%d | 热重载=%s", BASE_DIR, port, reload_on)
    uvicorn.run(app, host="0.0.0.0", port=port, reload=reload_on, log_config=None)


if __name__ == "__main__":
    main()
