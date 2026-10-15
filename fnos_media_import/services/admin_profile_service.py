from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ..repositories.admin_profile_repository import AdminProfileRepository


class UploadedFile(Protocol):
    filename: str

    def read(self) -> bytes: ...


class _ProfileUpdateError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class AdminProfileService:
    ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    MAX_IMAGE_BYTES = 5 * 1024 * 1024

    def __init__(
        self,
        repository: AdminProfileRepository,
        admin_config: dict[str, Any],
        upload_dir: Path,
        hash_password: Callable[[str], str],
        verify_password_hash: Callable[[str, str], bool],
        clock: Callable[[], float] = time.time,
        *,
        session_key: str | bytes,
    ) -> None:
        self._repository = repository
        self._admin_config = dict(admin_config)
        self._upload_dir = upload_dir
        self._hash_password = hash_password
        self._verify_password_hash = verify_password_hash
        self._clock = clock
        self._session_key = session_key.encode("utf-8") if isinstance(session_key, str) else session_key
        if not self._session_key:
            raise ValueError("管理员会话签名密钥不能为空")

    def _username(self, stored: dict[str, Any]) -> str:
        return str(stored.get("username") or self._admin_config.get("username") or "admin").strip() or "admin"

    @staticmethod
    def _same_text(left: Any, right: Any) -> bool:
        try:
            return hmac.compare_digest(str(left or "").encode("utf-8"), str(right or "").encode("utf-8"))
        except UnicodeError:
            return False

    def _session_fingerprint(self, stored: dict[str, Any]) -> str:
        password_hash = str(stored.get("password_hash") or "").strip()
        credential = password_hash or str(self._admin_config.get("password") or "admin")
        identity = [
            "admin-auth-v1", self._username(stored), "hash" if password_hash else "plain", credential,
            str(stored.get("auth_revision") or ""),
        ]
        # The cookie must not expose a password hash or an unkeyed password
        # digest. Derive a stable, cross-process revocation stamp with HMAC.
        encoded = json.dumps(identity, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._session_key, encoded, hashlib.sha256).hexdigest()

    def session_fingerprint(self) -> str:
        return self._session_fingerprint(self._repository.get_profile())

    def session_is_current(self, username: str, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        stored = self._repository.get_profile()
        return self._same_text(username, self._username(stored)) and self._same_text(
            fingerprint, self._session_fingerprint(stored)
        )

    def profile(self) -> dict[str, str]:
        return self._public_profile(self._repository.get_profile())

    def _public_profile(self, stored: dict[str, Any]) -> dict[str, str]:
        branding = self._repository.get_branding()
        username = self._username(stored)
        return {
            "username": username,
            "avatar_url": str(stored.get("avatar_url") or "").strip(),
            "logo_url": str(branding.get("logo_url") or "").strip(),
        }

    def verify_password(self, username: str, password: str) -> tuple[bool, str]:
        return self._verify_password(self._repository.get_profile(), username, password)

    def _verify_password(self, stored: dict[str, Any], username: str, password: str) -> tuple[bool, str]:
        expected_username = self._username(stored)
        if not self._same_text(username, expected_username):
            return False, expected_username
        password_hash = str(stored.get("password_hash") or "").strip()
        if password_hash:
            return self._verify_password_hash(str(password or ""), password_hash), expected_username
        expected_password = str(self._admin_config.get("password") or "admin")
        return self._same_text(password, expected_password), expected_username

    def update_profile(
        self,
        payload: dict[str, Any],
        current_username: str,
        *,
        current_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            return {"success": False, "message": "个人设置参数必须是 JSON 对象"}, 400
        username = str(payload.get("username") or "").strip()
        current_password = str(payload.get("current_password") or "")
        new_password = str(payload.get("new_password") or "")
        try:
            username.encode("utf-8")
            new_password.encode("utf-8")
        except UnicodeError:
            return {"success": False, "message": "用户名和密码必须是有效的 UTF-8 文本"}, 400
        if not username:
            return {"success": False, "message": "用户名不能为空"}, 400
        if len(username) > 64:
            return {"success": False, "message": "用户名不能超过 64 个字符"}, 400
        if new_password and len(new_password) < 6:
            return {"success": False, "message": "新密码至少 6 位"}, 400

        def _update(stored: dict[str, Any]) -> dict[str, Any]:
            # Recheck under the settings write lock: a request already past the
            # route guard must not overwrite or renew credentials changed by
            # another process while this request was in flight.
            if not self._same_text(current_username, self._username(stored)) or (
                current_fingerprint is not None
                and not self._same_text(current_fingerprint, self._session_fingerprint(stored))
            ):
                raise _ProfileUpdateError("登录状态已失效，请重新登录", 401)
            username_changed = username != self._username(stored)
            if (username_changed or new_password) and not self._verify_password(stored, current_username, current_password)[0]:
                raise _ProfileUpdateError("当前密码验证失败")
            updated = {**stored, "username": username}
            if new_password:
                updated["password_hash"] = self._hash_password(new_password)
            if username_changed or new_password:
                updated["auth_revision"] = secrets.token_hex(16)
            return updated

        try:
            updated = self._repository.update_profile_atomic(_update)
        except _ProfileUpdateError as exc:
            return {"success": False, "message": str(exc)}, exc.status_code
        return {
            "success": True, "message": "个人设置已保存", "profile": self._public_profile(updated),
            "_auth_fingerprint": self._session_fingerprint(updated),
        }, 200

    def save_avatar(self, file: UploadedFile | None) -> tuple[dict[str, Any], int]:
        result, status = self._save_image(file, "admin_avatar")
        if status != 200:
            return result, status
        updated = self._repository.update_profile_atomic(lambda stored: {**stored, "avatar_url": result["url"]})
        return {"success": True, "message": "头像已更新", "profile": self._public_profile(updated)}, 200

    def save_logo(self, file: UploadedFile | None) -> tuple[dict[str, Any], int]:
        result, status = self._save_image(file, "site_logo")
        if status != 200:
            return result, status
        updated = self._repository.get_branding()
        updated["logo_url"] = result["url"]
        self._repository.save_branding(updated)
        return {"success": True, "message": "网站 Logo 已更新", "profile": self.profile()}, 200

    def _save_image(self, file: UploadedFile | None, prefix: str) -> tuple[dict[str, Any], int]:
        if file is None or not getattr(file, "filename", ""):
            return {"success": False, "message": "请选择图片文件"}, 400
        suffix = Path(str(file.filename)).suffix.lower()
        if suffix not in self.ALLOWED_IMAGE_SUFFIXES:
            return {"success": False, "message": "仅支持 png/jpg/jpeg/webp/gif 图片"}, 400
        content = file.read()
        if len(content) > self.MAX_IMAGE_BYTES:
            return {"success": False, "message": "图片不能超过 5MB"}, 400
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        target = self._upload_dir / f"{prefix}{suffix}"
        target.write_bytes(content)
        url = f"/static/uploads/{target.name}?v={int(self._clock())}"
        return {"success": True, "url": url}, 200
