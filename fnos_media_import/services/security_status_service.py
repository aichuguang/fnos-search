from __future__ import annotations

from typing import Any, Callable


class SecurityStatusService:
    """Builds the admin-facing security posture summary."""

    def __init__(
        self,
        *,
        raw_config: Callable[[], dict[str, Any]],
        settings: Callable[[], dict[str, Any]],
        strict_enabled: Callable[[dict[str, Any]], bool],
        default_secret: Callable[[str], bool],
        docker_socket_mounted: Callable[[], bool],
        admin_profile_key: str,
        deployment_degraded: Callable[[], bool] | None = None,
    ) -> None:
        self.raw_config = raw_config
        self.settings = settings
        self.strict_enabled = strict_enabled
        self.default_secret = default_secret
        self.docker_socket_mounted = docker_socket_mounted
        self.admin_profile_key = admin_profile_key
        self.deployment_degraded = deployment_degraded or (lambda: False)

    def build(self) -> dict[str, Any]:
        raw = self.raw_config()
        admin = raw.get("admin", {}) if isinstance(raw.get("admin"), dict) else {}
        stored = self.settings().get(self.admin_profile_key, {})
        stored = stored if isinstance(stored, dict) else {}
        app = raw.get("app", {}) if isinstance(raw.get("app"), dict) else {}
        username = str(stored.get("username") or admin.get("username") or "admin")
        custom_password = bool(str(stored.get("password_hash") or "").strip())
        password = "" if custom_password else str(admin.get("password") or "")
        secret_key = str(app.get("secret_key") or "")
        strict = self.strict_enabled(raw)
        socket_mounted = self.docker_socket_mounted()
        degraded = bool(self.deployment_degraded())
        issues: list[dict[str, Any]] = []

        if username == "admin" and not custom_password and password == "admin":
            _issue(issues, "critical", "管理员仍使用默认账号密码", "默认 admin/admin 风险极高，请优先修改管理员凭据", "前往系统设置的个人设置修改")
        elif not custom_password and password in {"", "admin", "password", "123456"}:
            _issue(issues, "warn", "管理员密码强度偏低", "当前密码为空或属于常见弱口令", "设置独立高强度管理员密码")
        if self.default_secret(secret_key):
            _issue(issues, "critical", "应用签名密钥初始化失败", "默认会话签名密钥可能导致登录状态被伪造", "检查 data 目录写权限，或显式配置 APP_SECRET_KEY")
        if socket_mounted:
            _issue(issues, "critical", "检测到 Docker Socket 挂载", "Web 容器可控制宿主机 Docker，安全边界风险较高", "替换为无 Socket 的当前编排文件")
        if degraded:
            _issue(issues, "critical", "检测到旧版部署编排", "正式环境缺少 FNOS_PROCESS_ROLE，已进入安全降级模式", "替换当前版本 docker-compose.yml 后重新编排")

        critical_count = sum(item["level"] == "critical" for item in issues)
        warn_count = sum(item["level"] == "warn" for item in issues)
        default_admin = username == "admin" and not custom_password and password == "admin"
        return {
            "success": True,
            "status": "critical" if critical_count else "warn" if warn_count else "ok",
            "critical_count": critical_count,
            "warn_count": warn_count,
            "issues": issues,
            "flags": {
                "default_admin": default_admin,
                "default_secret": self.default_secret(secret_key),
                "strict_security": strict,
                "docker_socket_mounted": socket_mounted,
                "deployment_degraded": degraded,
            },
        }


def _issue(
    issues: list[dict[str, Any]],
    level: str,
    title: str,
    message: str,
    action: str = "",
) -> None:
    issues.append({"level": level, "title": title, "message": message, "action": action})
