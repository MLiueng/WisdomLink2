"""内置管理员认证（V1 边界假设 NFR-219：仅管理面守门，问答入口免登录）。

安全加固（阶段 0-1，S-01）：
  - JWT 携带 exp/iat/jti；默认 2 小时过期（WL2_TOKEN_TTL_MIN 可调）；
  - 启动/签发时拒绝公开弱密钥（默认占位、"test" 的 SHA-256 等广为人知测试向量）。
"""
import time
import uuid
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.config import get_settings

_bearer = HTTPBearer(auto_error=False)
_ADMIN = {"username": "admin", "password": get_settings().admin_pw}

# 公开已知弱密钥黑名单：默认占位 + "test"/"admin" 的 SHA-256 等，命中即拒绝签发
_WEAK_SECRETS = {
    "wl2-dev-secret",
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",  # sha256("test")
    "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918",  # sha256("admin")
    "secret", "admin", "123456",
}


def secret_strength_ok(secret: str) -> bool:
    """密钥强度判定：非空、非公开弱值/占位符、长度 >= 32。"""
    return (bool(secret) and secret not in _WEAK_SECRETS
            and not secret.startswith("change-me") and len(secret) >= 32)


def _check_secret(secret: str) -> None:
    """弱密钥拒绝：公开常量/占位符/过短密钥不允许签发令牌（S-01）。"""
    if not secret_strength_ok(secret):
        raise HTTPException(500, "服务密钥强度不足，请配置随机且不少于 32 位的 WL2_SECRET 后重启")


def create_token(username: str, ttl_min: int | None = None) -> str:
    cfg = get_settings()
    _check_secret(cfg.secret)
    now = int(time.time())
    ttl = (ttl_min or getattr(cfg, "token_ttl_min", 120)) * 60
    return jwt.encode({"sub": username, "role": "admin", "iat": now,
                       "exp": now + ttl, "jti": uuid.uuid4().hex},
                      cfg.secret, algorithm="HS256")


def login(username: str, password: str) -> str | None:
    if username == _ADMIN["username"] and password == _ADMIN["password"]:
        return create_token(username)
    return None


def require_admin(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    if cred is None:
        raise HTTPException(401, "未登录")
    try:
        # decode 自动校验 exp；弱密钥下旧默认密钥签发的令牌一并拒绝（密钥已轮换）
        payload = jwt.decode(cred.credentials, get_settings().secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "令牌已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(401, "令牌无效或已过期")
    if payload.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return payload["sub"]
