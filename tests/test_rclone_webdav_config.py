from __future__ import annotations

import json
import subprocess
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from fnos_media_import.blueprints.rclone import RcloneRouteContext, create_rclone_blueprint
from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.services.rclone_webdav_config_service import (
    RcloneWebdavConfigError,
    RcloneWebdavConfigService,
)


class FakeDockerRunner:
    def __init__(self, configs: dict[str, dict[str, Any]] | None = None) -> None:
        self.configs = json.loads(json.dumps(configs or {}))
        self.backup: dict[str, dict[str, Any]] | None = None
        self.calls: list[dict[str, Any]] = []
        self.fail_save = False
        self.fail_test = False
        self.dump_calls = 0
        self.fail_dump_on: int | None = None

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": list(command), "input": kwargs.get("input")})
        script = command[-1] if command else ""
        if command[-4:-2] == ["config", "dump"] or "dump" in command:
            self.dump_calls += 1
            if self.dump_calls == self.fail_dump_on:
                return self._result(command, returncode=1, stderr="dump failed")
            return self._result(command, stdout=json.dumps(self.configs))
        if script == RcloneWebdavConfigService._BACKUP_SCRIPT:
            self.backup = json.loads(json.dumps(self.configs))
            return self._result(command)
        if script == RcloneWebdavConfigService._RESTORE_SCRIPT:
            if self.backup is None:
                return self._result(command, returncode=1, stderr="backup missing")
            self.configs = json.loads(json.dumps(self.backup))
            return self._result(command)
        if script == RcloneWebdavConfigService._SAVE_SCRIPT:
            if self.fail_save:
                return self._result(command, returncode=1, stderr="save failed")
            values = str(kwargs.get("input") or "").split("\n")
            operation, remote, url, username, password = values[:5]
            existing_pass = str(self.configs.get(remote, {}).get("pass") or "")
            self.configs[remote] = {
                "type": "webdav",
                "url": url,
                "vendor": "other",
                "user": username,
                "pass": "obscured-value" if password else existing_pass,
            }
            self.configs[remote]["operation"] = operation
            return self._result(command)
        if "lsd" in command:
            if self.fail_test:
                return self._result(command, returncode=1, stderr="connection refused")
            return self._result(command, stdout="          -1 2026-08-05 00:00:00        -1 movies\n")
        return self._result(command, returncode=1, stderr="unexpected command")

    @staticmethod
    def _result(
        command: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class RcloneWebdavConfigServiceTests(unittest.TestCase):
    def service(self, runner: FakeDockerRunner) -> RcloneWebdavConfigService:
        return RcloneWebdavConfigService(
            lambda: {
                "container_name": "rclone-server",
                "exec_user": "10001:10001",
                "remote_name": "MP",
            },
            runner=runner,
        )

    def test_status_never_returns_obscured_password(self) -> None:
        runner = FakeDockerRunner(
            {"MP": {"type": "webdav", "url": "http://openlist:5244/dav", "user": "admin", "pass": "secret-obscured"}}
        )

        result = self.service(runner).status()

        self.assertTrue(result["configured"])
        self.assertTrue(result["password_set"])
        self.assertNotIn("pass", result)
        self.assertNotIn("secret-obscured", json.dumps(result))

    def test_create_uses_stdin_for_password_and_checks_connection(self) -> None:
        runner = FakeDockerRunner()
        password = "plain-password-$-value"

        result = self.service(runner).save(
            {
                "remote_name": "MP",
                "url": "http://host.docker.internal:5244/dav/",
                "username": "openlist-user",
                "password": password,
            }
        )

        self.assertEqual(result["connection_status"], "success")
        self.assertEqual(result["url"], "http://host.docker.internal:5244/dav")
        self.assertTrue(any(password in str(call["input"] or "") for call in runner.calls))
        self.assertTrue(all(password not in " ".join(call["command"]) for call in runner.calls))
        save_call = next(call for call in runner.calls if call["command"][-1] == RcloneWebdavConfigService._SAVE_SCRIPT)
        self.assertEqual(
            save_call["command"][:6],
            ["docker", "exec", "-i", "--user", "10001:10001", "rclone-server"],
        )
        self.assertTrue(
            all(
                call["command"][:4] == ["docker", "exec", "--user", "10001:10001"]
                or call["command"][:5] == ["docker", "exec", "-i", "--user", "10001:10001"]
                for call in runner.calls
            )
        )
        self.assertTrue(any("lsd" in call["command"] for call in runner.calls))

    def test_empty_password_keeps_existing_password(self) -> None:
        runner = FakeDockerRunner(
            {"MP": {"type": "webdav", "url": "http://old:5244/dav", "user": "old", "pass": "existing-obscured"}}
        )

        result = self.service(runner).save(
            {
                "remote_name": "MP",
                "url": "http://new:5244/dav",
                "username": "new-user",
                "password": "",
            }
        )

        self.assertTrue(result["password_set"])
        self.assertEqual(runner.configs["MP"]["pass"], "existing-obscured")

    def test_connection_failure_restores_previous_config(self) -> None:
        original = {"MP": {"type": "webdav", "url": "http://old:5244/dav", "user": "old", "pass": "old-pass"}}
        runner = FakeDockerRunner(original)
        runner.fail_test = True

        with self.assertRaisesRegex(RcloneWebdavConfigError, "已恢复保存前的配置") as caught:
            self.service(runner).save(
                {
                    "remote_name": "MP",
                    "url": "http://new:5244/dav",
                    "username": "new-user",
                    "password": "new-password",
                }
            )

        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(runner.configs, original)
        self.assertTrue(any(call["command"][-1] == RcloneWebdavConfigService._RESTORE_SCRIPT for call in runner.calls))

    def test_write_failure_also_restores_previous_config(self) -> None:
        original = {"MP": {"type": "webdav", "url": "http://old:5244/dav", "user": "old", "pass": "old-pass"}}
        runner = FakeDockerRunner(original)
        runner.fail_save = True

        with self.assertRaisesRegex(RcloneWebdavConfigError, "已恢复保存前的配置"):
            self.service(runner).save(
                {
                    "remote_name": "MP",
                    "url": "http://new:5244/dav",
                    "username": "new-user",
                    "password": "new-password",
                }
            )

        self.assertEqual(runner.configs, original)

    def test_post_save_status_failure_restores_previous_config(self) -> None:
        original = {"MP": {"type": "webdav", "url": "http://old:5244/dav", "user": "old", "pass": "old-pass"}}
        runner = FakeDockerRunner(original)
        runner.fail_dump_on = 2

        with self.assertRaisesRegex(RcloneWebdavConfigError, "已恢复保存前的配置"):
            self.service(runner).save(
                {
                    "remote_name": "MP",
                    "url": "http://new:5244/dav",
                    "username": "new-user",
                    "password": "new-password",
                }
            )

        self.assertEqual(runner.configs, original)

    def test_non_webdav_remote_is_not_overwritten(self) -> None:
        runner = FakeDockerRunner({"MP": {"type": "s3", "provider": "Other"}})

        with self.assertRaises(RcloneWebdavConfigError) as caught:
            self.service(runner).save(
                {"remote_name": "MP", "url": "http://openlist:5244/dav", "username": "admin", "password": "secret"}
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(any(call["command"][-1] == RcloneWebdavConfigService._BACKUP_SCRIPT for call in runner.calls))

    def test_invalid_names_urls_and_control_characters_are_rejected(self) -> None:
        service = self.service(FakeDockerRunner())
        invalid_payloads = (
            {"remote_name": "bad:name", "url": "http://openlist/dav", "username": "admin", "password": "secret"},
            {"remote_name": "MP", "url": "file:///config/rclone.conf", "username": "admin", "password": "secret"},
            {"remote_name": "MP", "url": "http://openlist/dav", "username": "bad\nuser", "password": "secret"},
            {"remote_name": "MP", "url": "http://admin:secret@openlist/dav", "username": "admin", "password": "secret"},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(RcloneWebdavConfigError):
                service.save(payload)

    def test_invalid_exec_user_is_rejected_before_docker_call(self) -> None:
        runner = FakeDockerRunner()
        service = RcloneWebdavConfigService(
            lambda: {"container_name": "rclone-server", "exec_user": "root;id", "remote_name": "MP"},
            runner=runner,
        )

        with self.assertRaisesRegex(RcloneWebdavConfigError, "执行用户配置不合法"):
            service.status()

        self.assertEqual(runner.calls, [])

    def test_backup_failure_returns_safe_docker_detail(self) -> None:
        class BackupFailureRunner(FakeDockerRunner):
            def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if command[-1] == RcloneWebdavConfigService._BACKUP_SCRIPT:
                    self.calls.append({"command": list(command), "input": kwargs.get("input")})
                    return self._result(command, returncode=1, stderr="cp: Permission denied")
                return super().__call__(command, **kwargs)

        service = self.service(BackupFailureRunner())

        with self.assertRaisesRegex(RcloneWebdavConfigError, "Permission denied"):
            service.save(
                {"remote_name": "MP", "url": "http://openlist:5244/dav", "username": "admin", "password": "secret"}
            )

    def test_test_endpoint_logic_checks_saved_remote(self) -> None:
        runner = FakeDockerRunner(
            {"MP": {"type": "webdav", "url": "http://openlist:5244/dav", "user": "admin", "pass": "obscured"}}
        )

        result = self.service(runner).test("MP")

        self.assertEqual(result["connection_status"], "success")
        self.assertEqual(result["message"], "WebDAV 连接成功")


class RcloneWebdavRouteAndUiTests(unittest.TestCase):
    def test_all_webdav_config_routes_are_admin_protected(self) -> None:
        app = Flask(__name__)
        handler_calls: list[str] = []

        def protected(_handler):
            def denied(*_args, **_kwargs):
                return jsonify({"success": False}), 401

            return denied

        def ok(*_args, **_kwargs):
            handler_calls.append("called")
            return jsonify({"success": True})

        app.register_blueprint(
            create_rclone_blueprint(
                RcloneRouteContext(admin_required=protected, handlers=defaultdict(lambda: ok))
            )
        )
        client = app.test_client()

        self.assertEqual(client.get("/api/admin/rclone/webdav-config").status_code, 401)
        self.assertEqual(client.post("/api/admin/rclone/webdav-config", json={}).status_code, 401)
        self.assertEqual(client.post("/api/admin/rclone/webdav-config/test", json={}).status_code, 401)
        self.assertEqual(handler_calls, [])

    def test_openapi_documents_webdav_config_routes(self) -> None:
        paths = get_openapi_spec()["paths"]
        self.assertIn("get", paths["/api/admin/rclone/webdav-config"])
        self.assertIn("post", paths["/api/admin/rclone/webdav-config"])
        self.assertIn("post", paths["/api/admin/rclone/webdav-config/test"])
        self.assertEqual(
            paths["/api/admin/rclone/webdav-config"]["post"]["security"],
            [{"cookieAuth": []}],
        )

    def test_admin_page_has_complete_webdav_configuration_controls(self) -> None:
        template = Path("templates/admin.html").read_text(encoding="utf-8")
        script = Path("static/admin.js").read_text(encoding="utf-8")
        bootstrap = Path("static/admin-bootstrap.js").read_text(encoding="utf-8")

        for element_id in (
            "rcloneWebdavUrl",
            "rcloneWebdavUsername",
            "rcloneWebdavPassword",
            "rcloneWebdavStatus",
            "saveRcloneWebdavBtn",
            "testRcloneWebdavBtn",
        ):
            self.assertIn(f'id="{element_id}"', template)
        self.assertIn("password_set ? \"已保存，留空则保留\"", script)
        self.assertIn('api("/api/admin/rclone/webdav-config"', script)
        self.assertIn('api("/api/admin/rclone/webdav-config/test"', script)
        self.assertIn('$("saveRcloneWebdavBtn")?.addEventListener', bootstrap)
        self.assertIn('$("testRcloneWebdavBtn")?.addEventListener', bootstrap)


if __name__ == "__main__":
    unittest.main()
