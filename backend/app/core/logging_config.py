"""统一日志（NFR-243）：控制台 + 滚动文件；请求中间件注入 trace-id 与耗时。"""
import logging
import logging.handlers
import time
import uuid
from pathlib import Path

_configured = False


def setup_logging(level: str = "INFO", log_dir: str = "./logs") -> logging.Logger:
    global _configured
    log = logging.getLogger("wl2")
    if _configured:
        return log
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_h = logging.handlers.RotatingFileHandler(Path(log_dir) / "wl2.log",
                                                  maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    log.addHandler(console)
    log.addHandler(file_h)
    _configured = True
    return log


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)
