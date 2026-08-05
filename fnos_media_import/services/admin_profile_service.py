from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ..repositories.admin_profile_repository import AdminProfileRepository


class UploadedFile(Protocol):
    filename: str

    def read(self) -> bytes: ...


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
    ) -> None:
        self._repository = repository
        self._admin_config = dict(admin_config)
        self._upload_dir = upload_dir
        self._hash_password = hash_password
        self._verify_password_hash = verify_password_hash
        self._clock = clock

    def profile(self) -> dict[str, str]:
        stored = self._repository.get_profile()
        branding = self._repository.get_branding()
        username = str(stored.get("username") or self._admin_config.get("username") or "admin").strip() or "admin"
        return {
            "username": username,
            "avatar_url": str(stored.get("avatar_url") or "").strip(),
            "logo_url": str(branding.get("logo_url") or "").strip(),
        }

    def verify_password(self, username: str, password: str) -> tuple[bool, str]:
        stored = self._repository.get_profile()
        expected_username = str(stored.get("username") or self._admin_config.get("username") or "admin")
        if not hmac.compare_digest(str(username or ""), expected_username):
            return False, expected_username
        password_hash = str(stored.get("password_hash") or "").strip()
        if password_hash:
            return self._verify_password_hash(str(password or ""), password_hash), expected_username
        expected_password = str(self._admin_config.get("password") or "admin")
        return hmac.compare_digest(str(password or ""), expected_password), expected_username

    def update_profile(self, payload: dict[str, Any], current_username: str) -> tuple[dict[str, Any], int]:
        username = str(payload.get("username") or "").strip()
        current_password = str(payload.get("current_password") or "")
        new_password = str(payload.get("new_password") or "")
        if not username:
            return {"success": False, "message": "用户名不能为空"}, 400
        if len(username) > 64:
            return {"success": False, "message": "用户名不能超过 64 个字符"}, 400
        if new_password and len(new_password) < 6:
            return {"success": False, "message": "新密码至少 6 位"}, 400
        username_changed = username != self.profile()["username"]
        verified, _ = self.verify_password(current_username, current_password)
        if (username_changed or new_password) and not verified:
            return {"success": False, "message": "当前密码验证失败"}, 400
        updated = self._repository.get_profile()
        updated["username"] = username
        if new_password:
            updated["password_hash"] = self._hash_password(new_password)
        self._repository.save_profile(updated)
        return {"success": True, "message": "个人设置已保存", "profile": self.profile()}, 200

    def save_avatar(self, file: UploadedFile | None) -> tuple[dict[str, Any], int]:
        result, status = self._save_image(file, "admin_avatar")
        if status != 200:
            return result, status
        updated = self._repository.get_profile()
        updated["avatar_url"] = result["url"]
        self._repository.save_profile(updated)
        return {"success": True, "message": "头像已更新", "profile": self.profile()}, 200

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
