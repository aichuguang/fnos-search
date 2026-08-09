"""Notification secret storage using the application root key.

Environment references (``env:NAME``) remain the preferred storage form.
Literal values are encrypted as ``enc:v2:`` envelopes with the notification
subkey derived from ``FNOS_ROOT_KEY``.  No secondary notification key or
legacy envelope format is accepted.
"""

from __future__ import annotations

import base64
import os
import secrets as _secrets

from ..accounts.crypto import (
    PURPOSE_NOTIFICATION,
    ROOT_KEY_ENV,
    MasterKeyError,
    derive_key,
    master_key_from_env,
)

ENV_PREFIX = "env:"
ENC_PREFIX = "enc:v2:"
V2_ENC_PREFIX = ENC_PREFIX


def is_env_ref(value: object) -> bool:
    return str(value or "").strip().startswith(ENV_PREFIX)


def is_encrypted(value: object) -> bool:
    return str(value or "").strip().startswith(ENC_PREFIX)


def _root_key_bytes(key: str | bytes | bytearray | None = None) -> bytes:
    """Resolve the root key, optionally accepting a test-only raw override."""
    if key is None:
        try:
            return master_key_from_env()
        except MasterKeyError as exc:
            raise ValueError(f"未配置 {ROOT_KEY_ENV}，无法加密保存通知凭据") from exc
    if isinstance(key, (bytes, bytearray)):
        raw = bytes(key)
        if len(raw) != 32:
            raise ValueError(f"{ROOT_KEY_ENV} 必须是 32 字节根密钥")
        return raw
    text = str(key).strip()
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError("显式通知根密钥必须是 64 位十六进制字符串") from exc
    if len(raw) != 32:
        raise ValueError("显式通知根密钥必须是 32 字节")
    return raw


def encrypt(plaintext: object, key: str | bytes | bytearray | None = None) -> str:
    from Crypto.Cipher import AES

    secret = str(plaintext or "")
    root_key = _root_key_bytes(key)
    key_b = derive_key(root_key, PURPOSE_NOTIFICATION)
    nonce = _secrets.token_bytes(12)
    cipher = AES.new(key_b, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(secret.encode("utf-8"))
    payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return f"{ENC_PREFIX}{payload}"


def decrypt(encrypted: object, key: str | bytes | bytearray | None = None) -> str:
    from Crypto.Cipher import AES

    text = str(encrypted or "").strip()
    if text.startswith("enc:") and not text.startswith(ENC_PREFIX):
        return ""
    if not text.startswith(ENC_PREFIX):
        return text
    try:
        key_b = derive_key(_root_key_bytes(key), PURPOSE_NOTIFICATION)
    except (MasterKeyError, ValueError):
        return ""
    payload_text = text[len(ENC_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(payload_text.encode("ascii"))
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


def resolve(value: object, key: str | bytes | bytearray | None = None) -> str:
    """把存储值解析为真实凭据。env 引用读环境变量；密文解密；否则原样。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(ENV_PREFIX):
        return os.environ.get(text[len(ENV_PREFIX):].strip(), "")
    if text.startswith("enc:"):
        return decrypt(text, key)
    return text


def store(value: object, key: str | bytes | bytearray | None = None) -> str:
    """把用户输入转成可入库值：env 引用原样保留，真实值加密。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if is_env_ref(text):
        return text
    return encrypt(text, key)


def key_bytes(key: str | bytes | bytearray | None = None) -> bytes:
    """返回通知子密钥（由 FNOS_ROOT_KEY 派生，32 字节）。

    供 HMAC/校验等需要稳定密钥字节的场景使用；普通加解密走 ``store``/``resolve``。
    根密钥缺失时抛 ``ValueError``，调用方应据此把通知功能标记为不可用。
    """
    return derive_key(_root_key_bytes(key), PURPOSE_NOTIFICATION)
