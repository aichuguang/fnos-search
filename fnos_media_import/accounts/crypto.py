"""Provider 账号凭证的 AES-256-GCM 信封加密。

主密钥只通过环境变量 ``FNOS_ACCOUNT_MASTER_KEY``（十六进制，64 字符）提供。
缺少主密钥时抛出明确错误，绝不允许退化为明文保存。

密文信封布局：``nonce(12) || tag(16) || ciphertext``。key_id 当前固定为 ``v1``，
用于将来轮换主密钥时区分版本。
"""

from __future__ import annotations

import os
from typing import Any

from Crypto.Cipher import AES

DEFAULT_KEY_ID = "v1"
MASTER_KEY_ENV = "FNOS_ACCOUNT_MASTER_KEY"

_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32  # AES-256


class MasterKeyError(RuntimeError):
    """账号主密钥信封相关的错误基类。"""


class MasterKeyMissingError(MasterKeyError):
    """环境变量 FNOS_ACCOUNT_MASTER_KEY 未配置。"""


class UnknownKeyIdError(MasterKeyError):
    """密文引用了未知的密钥版本。"""


class DecryptionError(MasterKeyError):
    """密文无法认证或解密（主密钥不匹配或密文被篡改）。"""


def master_key_from_env(environ: dict[str, str] | None = None) -> bytes:
    """从环境读取并解码 32 字节 AES-256 主密钥。

    ``environ`` 为 None 时读取真实进程环境；传入 dict 时读取该 dict（用于测试）。
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(MASTER_KEY_ENV) or "").strip()
    if not raw:
        raise MasterKeyMissingError(
            f"未配置环境变量 {MASTER_KEY_ENV}，Provider 账号凭证无法加密保存"
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise MasterKeyError(f"{MASTER_KEY_ENV} 必须是十六进制字符串") from exc
    if len(key) != _KEY_LEN:
        raise MasterKeyError(
            f"{MASTER_KEY_ENV} 必须是 32 字节（64 位十六进制字符）的 AES-256 主密钥"
        )
    return key


def encrypt(master_key: bytes, plaintext: str | bytes) -> tuple[bytes, str]:
    """加密明文，返回 ``(ciphertext_envelope, key_id)``。"""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext, DEFAULT_KEY_ID


def decrypt(master_key: bytes, key_id: str, ciphertext: bytes) -> bytes:
    """按 key_id 解封并认证密文，返回明文 bytes。

    主密钥不匹配或密文被篡改时抛出 :class:`DecryptionError`。
    """
    if key_id != DEFAULT_KEY_ID:
        raise UnknownKeyIdError(f"不支持的凭证密钥版本：{key_id}")
    if not ciphertext:
        raise DecryptionError("凭证密文为空")
    if len(ciphertext) < _NONCE_LEN + _TAG_LEN:
        raise DecryptionError("凭证密文不完整")
    nonce = ciphertext[:_NONCE_LEN]
    tag = ciphertext[_NONCE_LEN : _NONCE_LEN + _TAG_LEN]
    body = ciphertext[_NONCE_LEN + _TAG_LEN :]
    try:
        cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(body, tag)
    except ValueError as exc:
        raise DecryptionError("凭证解密失败：主密钥不匹配或密文被篡改") from exc


__all__ = [
    "DEFAULT_KEY_ID",
    "DecryptionError",
    "MASTER_KEY_ENV",
    "MasterKeyError",
    "MasterKeyMissingError",
    "UnknownKeyIdError",
    "decrypt",
    "encrypt",
    "master_key_from_env",
]
