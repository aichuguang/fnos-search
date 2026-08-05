from __future__ import annotations

from typing import Any

from ..constants import JOB_FAILED, JOB_SUBMITTED
from ..http_client import HttpClient
from .base import AdapterCheckResult, ImportResult


class GenericWebhookImporter:
    """通用 Webhook 入库适配器。

    后续 139 / 天翼 / 6盘 如果已有 HTTP 服务，但接口细节暂未固定，可以先用
    这个适配器承接：系统会把标准字段 POST 给配置的 endpoint。
    """

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.endpoint = str(config.get("endpoint") or config.get("auto_save_url") or config.get("api_url") or "").strip()
        self.token = str(config.get("token") or "").strip()
        self.timeout = int(config.get("timeout", 20) or 20)
        self.status_url = str(config.get("status_url") or config.get("health_url") or "").strip()
        self.task_status_url = str(config.get("task_status_url") or config.get("poll_url") or "").strip()
        self.success_field = str(config.get("success_field") or "success")
        self.refresh_after_submit = bool(config.get("refresh_after_submit", False))
        self.http = HttpClient(timeout=self.timeout, verify_tls=False)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter_type": "generic_webhook",
            "configured": bool(self.endpoint),
            "capabilities": {
                "submit": bool(self.endpoint),
                "status_probe": bool(self.status_url),
                "task_poll": bool(self.task_status_url),
                "refresh_after_submit": self.refresh_after_submit,
            },
            "config": {
                "endpoint_configured": bool(self.endpoint),
                "token_configured": bool(self.token),
                "status_url_configured": bool(self.status_url),
                "task_status_url_configured": bool(self.task_status_url),
                "timeout": self.timeout,
            },
        }

    def check_configuration(self) -> AdapterCheckResult:
        if not self.endpoint:
            return AdapterCheckResult(False, "unconfigured", f"{self.name} 入库接口未配置")
        return AdapterCheckResult(True, "ready", f"{self.name} 通用 Webhook 已配置", self.describe())

    def probe(self) -> AdapterCheckResult:
        # 占位检查只做本地配置判断，不主动调用第三方接口。
        # 139 / 天翼 / 磁链后续接入专用适配器后，再把真实连通性探测放到对应实现里。
        config_result = self.check_configuration()
        if not config_result.ok:
            return config_result
        return AdapterCheckResult(True, "placeholder", f"{self.name} 对接入口已预留，尚未执行真实平台探测", self.describe())

    def import_resource(self, title: str, source_url: str, category: dict[str, Any], password: str = "") -> ImportResult:
        if not self.endpoint:
            return ImportResult(False, JOB_FAILED, f"{self.name} 入库接口未配置")

        payload = {
            "title": title,
            "url": source_url,
            "shareurl": source_url,
            "password": password,
            "category": category.get("label"),
            "target_path": category.get("mobile_target_path") or category.get("quark_save_path"),
            "fnos_lib": category.get("fnos_lib") or category.get("label"),
        }
        url = self.endpoint
        if self.token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self.token}"

        status_code, data = self.http.post_json(url, payload, timeout=self.timeout)
        ok = status_code < 400
        if isinstance(data, dict) and self.success_field in data:
            ok = bool(data.get(self.success_field))

        if not ok:
            return ImportResult(
                False,
                JOB_FAILED,
                f"{self.name} 入库提交失败：HTTP {status_code}",
                target_path=str(payload["target_path"] or ""),
                raw_data={"response": data, "request": payload},
            )

        external_task_id = ""
        if isinstance(data, dict):
            external_task_id = str(data.get("id") or data.get("task_id") or data.get("data", {}).get("id") or "")

        return ImportResult(
            True,
            JOB_SUBMITTED,
            f"已提交 {self.name} 入库任务",
            external_task_id=external_task_id,
            target_path=str(payload["target_path"] or ""),
            raw_data={"response": data, "request": payload},
        )
