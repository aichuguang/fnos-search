"""Versioned AES-256-GCM envelopes derived from one application root key.

Every persisted secret is encrypted with an HKDF-SHA256 subkey scoped to its
purpose.  The only accepted root-key source is ``FNOS_ROOT_KEY`` and every
ciphertext must carry a ``v2:<purpose>`` key id.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Iterable

from Crypto.Cipher import AES

ROOT_KEY_ENV = "FNOS_ROOT_KEY"

PURPOSE_PROVIDER_ACCOUNT = "provider-account"
PURPOSE_AUTH_SESSION = "provider-auth-session"
PURPOSE_APP_SESSION = "app-session"
PURPOSE_SOURCE_RECORD = "source-record"
PURPOSE_OPENLIST = "openlist"
PURPOSE_NOTIFICATION = "notification"

DEFAULT_KEY_ID = f"v2:{PURPOSE_PROVIDER_ACCOUNT}"

_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32
_HKDF_SALT = b"fnos-search:root-key:v2"
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class MasterKeyError(RuntimeError):
    """Root-key envelope error."""


class MasterKeyMissingError(MasterKeyError):
    """The required root key is not configured."""


class UnknownKeyIdError(MasterKeyError):
    """The ciphertext references an unsupported key derivation version."""


class DecryptionError(MasterKeyError):
    """The ciphertext cannot be authenticated with any compatible key."""


def master_key_from_env(environ: dict[str, str] | None = None) -> bytes:
    """Load and validate the 32-byte root key from ``FNOS_ROOT_KEY``."""
    env = os.environ if environ is None else environ
    raw = str(env.get(ROOT_KEY_ENV) or "").strip()
    if not raw:
        raise MasterKeyMissingError(
            f"未配置环境变量 {ROOT_KEY_ENV}，无法加密保存敏感数据"
        )
    return _decode_hex_key(raw, ROOT_KEY_ENV)


def derive_key(root_key: bytes, purpose: str) -> bytes:
    """Derive an isolated 32-byte purpose key using HKDF-SHA256."""
    root = bytes(root_key or b"")
    if len(root) != _KEY_LEN:
        raise MasterKeyError("根密钥必须是 32 字节（64 位十六进制字符）")
    purpose_name = str(purpose or "").strip()
    if not _PURPOSE_RE.fullmatch(purpose_name):
        raise MasterKeyError(f"非法的密钥用途：{purpose_name or '空'}")
    prk = hmac.new(_HKDF_SALT, root, hashlib.sha256).digest()
    return hmac.new(prk, f"fnos-search:{purpose_name}:v2".encode("ascii") + b"\x01", hashlib.sha256).digest()


def encrypt(
    master_key: bytes,
    plaintext: str | bytes,
    *,
    purpose: str = PURPOSE_PROVIDER_ACCOUNT,
) -> tuple[bytes, str]:
    """Encrypt plaintext with a purpose-derived subkey and return envelope + key id."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    cipher = AES.new(derive_key(master_key, purpose), AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + tag + ciphertext, f"v2:{purpose}"


def decrypt(
    master_key: bytes,
    key_id: str,
    ciphertext: bytes,
    *,
    purpose: str | None = None,
) -> bytes:
    """Decrypt a v2 purpose envelope."""
    identifier = str(key_id or "")
    if not identifier.startswith("v2:"):
        raise UnknownKeyIdError(f"不支持的凭证密钥版本：{identifier}")
    stored_purpose = identifier[3:]
    if not _PURPOSE_RE.fullmatch(stored_purpose):
        raise UnknownKeyIdError(f"不支持的凭证密钥版本：{identifier}")
    if purpose and stored_purpose != purpose:
        raise DecryptionError(f"密文用途不匹配：期望 {purpose}，实际 {stored_purpose}")
    raw = bytes(ciphertext or b"")
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise DecryptionError("凭证密文不完整")
    return _decrypt_with_candidates(raw, [derive_key(master_key, stored_purpose)])


def _decrypt_with_candidates(raw: bytes, candidates: Iterable[bytes]) -> bytes:
    nonce, tag, body = raw[:_NONCE_LEN], raw[_NONCE_LEN : _NONCE_LEN + _TAG_LEN], raw[_NONCE_LEN + _TAG_LEN :]
    for key in candidates:
        try:
            cipher = AES.new(bytes(key), AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(body, tag)
        except (ValueError, KeyError):
            continue
    raise DecryptionError("凭证解密失败：根密钥不匹配或密文被篡改")


def _decode_hex_key(raw: str, name: str) -> bytes:
    try:
        key = bytes.fromhex(str(raw or "").strip())
    except ValueError as exc:
        raise MasterKeyError(f"{name} 必须是十六进制字符串") from exc
    if len(key) != _KEY_LEN:
        raise MasterKeyError(f"{name} 必须是 32 字节（64 位十六进制字符）的根密钥")
    return key


__all__ = [
    "DEFAULT_KEY_ID",
    "DecryptionError",
    "MasterKeyError",
    "MasterKeyMissingError",
    "PURPOSE_APP_SESSION",
    "PURPOSE_AUTH_SESSION",
    "PURPOSE_NOTIFICATION",
    "PURPOSE_OPENLIST",
    "PURPOSE_PROVIDER_ACCOUNT",
    "PURPOSE_SOURCE_RECORD",
    "ROOT_KEY_ENV",
    "UnknownKeyIdError",
    "decrypt",
    "derive_key",
    "encrypt",
    "master_key_from_env",
]
