from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.rclone_webdav_config_service import (
    RCLONE_UNAVAILABLE,
    RcloneWebdavConfigError,
)

MSG_REMOTE_REBUILT = "已按 OpenList 连接"
MSG_REMOTE_MISSING_HINT = "存在多个不同地址的 OpenList 连接"


class FakeWebdavService:
    def __init__(self, state=None, *, error=None, save_error=None):
        self.state = state
        self.error = error
        self.save_error = save_error
        self.saved: list[dict] = []

    def status(self, remote_name=None):
        if self.error is not None:
            raise self.error
        return dict(self.state or {})

    def save(self, payload):
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(dict(payload))
        return {"success": True, "configured": True}


class FakeConnectionService:
    def __init__(self, rows=None, credentials=None, *, list_error=None):
        self.rows = rows or []
        self.credentials = credentials or {}
        self.list_error = list_error
        self.resolved: list[tuple[str, int]] = []

    def list_connections(self):
        if self.list_error is not None:
            raise self.list_error
        return [dict(row) for row in self.rows]

    def resolve_credentials(self, connection_id, revision=None):
        self.resolved.append((connection_id, int(revision or 0)))
        return dict(self.credentials)


def _service(webdav, connections) -> tuple[RcloneService, list[str]]:
    service = RcloneService.__new__(RcloneService)
    service._v1_locator_resolver = SimpleNamespace(remote_name="MP")
    service._v1_webdav_config_service = webdav
    service._v1_connection_service = connections
    logs: list[str] = []
    service._append_log = logs.append
    return service, logs


def _connection_row(**overrides) -> dict:
    row = {
        "connection_id": "ol-1",
        "name": "主 OpenList",
        "base_url": "http://openlist:5244/dav",
        "username": "admin",
        "credential_configured": True,
        "enabled": True,
        "revision": 2,
    }
    row.update(overrides)
    return row


class ManagedRemoteAutoprovisionTests(unittest.TestCase):
    def test_missing_remote_is_rebuilt_from_database_credentials(self) -> None:
        webdav = FakeWebdavService({"configured": False})
        connections = FakeConnectionService(
            [_connection_row()],
            {
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret-pass",
            },
        )
        service, logs = _service(webdav, connections)

        service._ensure_managed_remote_config()

        self.assertEqual(len(webdav.saved), 1)
        payload = webdav.saved[0]
        self.assertEqual(
            payload,
            {
                "remote_name": "MP",
                "url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret-pass",
                "_connection_name": "主 OpenList",
            },
        )
        self.assertEqual(connections.resolved, [("ol-1", 2)])
        self.assertTrue(any(MSG_REMOTE_REBUILT in line for line in logs))

    def test_configured_remote_skips_save(self) -> None:
        webdav = FakeWebdavService({"configured": True, "remote_name": "MP"})
        service, logs = _service(webdav, FakeConnectionService())

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertEqual(logs, [])

    def test_rclone_unavailable_degrades_silently(self) -> None:
        webdav = FakeWebdavService(
            error=RcloneWebdavConfigError("rclone 管理命令不可用", 503, code=RCLONE_UNAVAILABLE)
        )
        service, logs = _service(webdav, FakeConnectionService())

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertEqual(logs, [])

    def test_status_failure_is_logged_without_blocking(self) -> None:
        webdav = FakeWebdavService(error=RuntimeError("boom"))
        service, logs = _service(webdav, FakeConnectionService())

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertTrue(any("检查托管 rclone Remote" in line for line in logs))

    def test_save_failure_is_logged_without_raising(self) -> None:
        webdav = FakeWebdavService({"configured": False}, save_error=RuntimeError("write denied"))
        connections = FakeConnectionService(
            [_connection_row()],
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "p"},
        )
        service, logs = _service(webdav, connections)

        service._ensure_managed_remote_config()

        self.assertTrue(any("自动重建托管 rclone Remote" in line for line in logs))

    def test_no_candidate_connection_is_a_noop(self) -> None:
        webdav = FakeWebdavService({"configured": False})
        service, logs = _service(
            webdav, FakeConnectionService([_connection_row(enabled=False)])
        )

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertEqual(logs, [])

    def test_ambiguous_base_urls_refuse_to_guess(self) -> None:
        webdav = FakeWebdavService({"configured": False})
        connections = FakeConnectionService(
            [
                _connection_row(),
                _connection_row(
                    connection_id="ol-2", base_url="http://other-openlist:5244/dav"
                ),
            ]
        )
        service, logs = _service(webdav, connections)

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertTrue(any(MSG_REMOTE_MISSING_HINT in line for line in logs))

    def test_same_base_url_multiple_connections_uses_first(self) -> None:
        webdav = FakeWebdavService({"configured": False})
        connections = FakeConnectionService(
            [
                _connection_row(),
                _connection_row(connection_id="ol-2", name="备用连接", revision=5),
            ],
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": "p"},
        )
        service, logs = _service(webdav, connections)

        service._ensure_managed_remote_config()

        self.assertEqual(len(webdav.saved), 1)
        self.assertEqual(connections.resolved, [("ol-1", 2)])

    def test_missing_password_aborts_save(self) -> None:
        webdav = FakeWebdavService({"configured": False})
        connections = FakeConnectionService(
            [_connection_row()],
            {"base_url": "http://openlist:5244/dav", "username": "admin", "password": ""},
        )
        service, logs = _service(webdav, connections)

        service._ensure_managed_remote_config()

        self.assertEqual(webdav.saved, [])
        self.assertEqual(logs, [])

    def test_unbound_services_are_a_noop(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service._v1_locator_resolver = SimpleNamespace(remote_name="MP")
        service._v1_webdav_config_service = None
        service._v1_connection_service = None
        logs: list[str] = []
        service._append_log = logs.append

        service._ensure_managed_remote_config()

        self.assertEqual(logs, [])


class AutoprovisionWiringContractTests(unittest.TestCase):
    """自检必须在 run 启动前执行，且由 app 组装根绑定服务。"""

    def test_run_script_invokes_autoprovision_before_spawn(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "fnos_media_import" / "services" / "rclone_service.py").read_text(encoding="utf-8")
        run_script = source[source.index("def _run_script(") : source.index("def _set_process(")]
        ensure_call = run_script.index("self._ensure_managed_remote_config()")
        spawn_call = run_script.index("self.process_runner.run(")
        self.assertLess(ensure_call, spawn_call)

    def test_app_assembly_binds_webdav_config_service(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "fnos_media_import" / "app.py").read_text(encoding="utf-8")
        self.assertIn("rclone_service.set_webdav_config_service(rclone_webdav_config_service)", source)


if __name__ == "__main__":
    unittest.main()
