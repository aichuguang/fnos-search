"""通知凭据处理：env 引用优先，必要时用 AES-GCM 加密落库。

安全模型：掩码只在接口返回时生效，落库必须避免明文。第一优先是
``env:环境变量名`` 引用（数据库只存引用，不存值）；若必须在后台录入
真实密码，则用 ``NOTIFICATION_ENCRYPTION_KEY`` 派生 AES-GCM 密钥。
它可以是任意非空口令，也兼容旧版原始密钥和 ``base64:`` 密钥。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets

ENV_PREFIX = "env:"
ENC_PREFIX = "enc:"
_KDF_SALT = b"fnos-media-import:notifications:v1"
_KDF_ITERATIONS = 200_000


def is_env_ref(value: object) -> bool:
    return str(value or "").strip().startswith(ENV_PREFIX)


def is_encrypted(value: object) -> bool:
    return str(value or "").strip().startswith(ENC_PREFIX)


def key_bytes(key: str | None = None) -> bytes | None:
    raw = str(key if key is not None else os.environ.get("NOTIFICATION_ENCRYPTION_KEY", "")).strip()
    if not raw:
        return None
    if raw.startswith("base64:"):
        try:
            decoded = base64.urlsafe_b64decode(raw[len("base64:"):].encode("ascii"))
        except Exception as exc:
            raise ValueError("NOTIFICATION_ENCRYPTION_KEY 的 Base64 格式不正确") from exc
        if len(decoded) not in (16, 24, 32):
            raise ValueError("base64: 密钥解码后必须是 16/24/32 字节")
        return decoded
    raw_bytes = raw.encode("utf-8")
    if len(raw_bytes) in (16, 24, 32):
        return raw_bytes
    return hashlib.pbkdf2_hmac(
        "sha256",
        raw_bytes,
        _KDF_SALT,
        _KDF_ITERATIONS,
        dklen=32,
    )


def encrypt(plaintext: object, key: str | None = None) -> str:
    from Crypto.Cipher import AES

    secret = str(plaintext or "")
    key_b = key_bytes(key)
    if key_b is None:
        raise ValueError("未配置 NOTIFICATION_ENCRYPTION_KEY，无法加密保存凭据；请改用 env:环境变量引用")
    nonce = _secrets.token_bytes(12)
    cipher = AES.new(key_b, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(secret.encode("utf-8"))
    payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return f"{ENC_PREFIX}{payload}"


def decrypt(encrypted: object, key: str | None = None) -> str:
    from Crypto.Cipher import AES

    text = str(encrypted or "").strip()
    if not text.startswith(ENC_PREFIX):
        return text
    key_b = key_bytes(key)
    if key_b is None:
        return ""
    try:
        raw = base64.urlsafe_b64decode(text[len(ENC_PREFIX):].encode("ascii"))
    except Exception:
        return ""
    if len(raw) < 12 + 16:
        return ""
    nonce, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
    try:
        cipher = AES.new(key_b, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception:
        return ""


def resolve(value: object, key: str | None = None) -> str:
    """把存储值解析为真实凭据。env 引用读环境变量；密文解密；否则原样。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(ENV_PREFIX):
        return os.environ.get(text[len(ENV_PREFIX):].strip(), "")
    if text.startswith(ENC_PREFIX):
        return decrypt(text, key)
    return text


def store(value: object, key: str | None = None) -> str:
    """把用户输入转成可入库值：env 引用原样保留，真实值加密。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if is_env_ref(text):
        return text
    return encrypt(text, key)
