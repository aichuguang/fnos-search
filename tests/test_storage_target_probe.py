from __future__ import annotations

import unittest
from typing import Any

from fnos_media_import.services.storage_target_probe_service import StorageTargetProbeService
from fnos_media_import.storage.types import StorageTarget


def _target(destination_id: str, driver: str, **config: Any) -> StorageTarget:
    return StorageTarget(
        destination_id=destination_id,
        name=destination_id,
        driver=driver,
        enabled=True,
        guest_selectable=False,
        config=config,
    )


class StorageTargetProbeTests(unittest.TestCase):
    def test_rclone_probe_reads_and_cleans_unique_temporary_directory(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> dict[str, Any]:
            commands.append(command)
            return {"ok": True, "message": "ok"}

        service = StorageTargetProbeService(
            rclone_config=lambda: {"config_path": "/config/rclone.conf", "cache_dir": "/cache"},
            command_runner=runner,
        )
        result = service.probe(_target("archive", "rclone", remote="archive_remote"))
        self.assertTrue(result["success"])
        self.assertEqual([command[1] for command in commands], ["lsd", "mkdir", "rmdir", "about"])
        self.assertEqual(commands[1][2], commands[2][2])
        self.assertIn(".fnos-storage-probe-", commands[1][2])

    def test_rclone_probe_attempts_cleanup_and_fails_when_cleanup_fails(self) -> None:
        def runner(command: list[str]) -> dict[str, Any]:
            return {"ok": command[1] != "rmdir", "message": command[1]}

        service = StorageTargetProbeService(rclone_config=lambda: {}, command_runner=runner)
        result = service.probe(_target("archive", "rclone", remote="archive_remote"))
        self.assertFalse(result["success"])
        self.assertTrue(any(item["name"] == "临时写入与删除" and not item["ok"] for item in result["checks"]))

    def test_cmcc_probe_reports_unsupported_checks_without_false_failure(self) -> None:
        class Session:
            closed = False

            def close(self) -> None:
                self.closed = True

        class Client:
            def __init__(self) -> None:
                self.session = Session()

            @staticmethod
            def list_dir(_root: str, *, page_size: int) -> list[dict[str, Any]]:
                return []

        client = Client()
        service = StorageTargetProbeService(
            rclone_config=lambda: {},
            cmcc_client_factory=lambda _config: client,
        )
        result = service.probe(_target("mobile", "cmcc_api", host="https://example.test", access_token="Basic abc"))
        self.assertTrue(result["success"])
        self.assertTrue(client.session.closed)
        unsupported = [item for item in result["checks"] if not item["required"]]
        self.assertEqual(len(unsupported), 2)


if __name__ == "__main__":
    unittest.main()
