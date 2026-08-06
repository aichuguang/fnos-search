from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
from flask import Flask, jsonify

from fnos_media_import.blueprints.rclone_worker_control import (
    RcloneWorkerControlContext,
    create_rclone_worker_control_blueprint,
)
from fnos_media_import.services.rclone_environment import RcloneEnvironmentChecker
from fnos_media_import.services.rclone_admin_service import (
    RcloneAdminQueryDependencies,
    RcloneAdminQueryService,
)
from fnos_media_import.services.rclone_file_retry_service import (
    RcloneFileRetryDependencies,
    RcloneFileRetryService,
)
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigService
from fnos_media_import.services.rclone_worker_client import (
    RcloneWorkerClient,
    RcloneWorkerRequestError,
)


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.trust_env = True
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response

    def close(self) -> None:
        self.closed = True


class _SequenceSession(_Session):
    def __init__(self, outcomes: list[Any]) -> None:
        super().__init__(_Response({"success": False}, 500))
        self.outcomes = list(outcomes)

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RcloneWorkerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(
            create_rclone_worker_control_blueprint(
                RcloneWorkerControlContext(
                    token=lambda: "stable-token",
                    handlers={
                        "status": lambda: jsonify({"success": True, "status": {"running": False}}),
                        "worker_status": lambda: jsonify(
                            {"success": True, "healthy": True, "status": "ready"}
                        ),
                    },
                )
            )
        )
        self.client = app.test_client()

    def test_internal_route_requires_bearer_token(self) -> None:
        self.assertEqual(self.client.get("/api/internal/rclone/status").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/internal/rclone/status",
                headers={"Authorization": "stable-token"},
            ).status_code,
            401,
        )

    def test_internal_route_accepts_matching_bearer_token(self) -> None:
        response = self.client.get(
            "/api/internal/rclone/status",
            headers={"Authorization": "Bearer stable-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["status"]["running"])

    def test_worker_status_route_uses_the_same_bearer_authentication(self) -> None:
        self.assertEqual(self.client.get("/api/internal/worker/status").status_code, 401)
        response = self.client.get(
            "/api/internal/worker/status",
            headers={"Authorization": "Bearer stable-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["healthy"])


class RcloneWorkerClientTests(unittest.TestCase):
    def test_client_sends_token_and_disables_proxy_environment(self) -> None:
        session = _Session(_Response({"success": True, "status": {"running": True}}))
        client = RcloneWorkerClient(
            base_url="http://worker:5251/",
            token="secret",
            database=object(),
            session=session,
        )

        self.assertTrue(client.status()["running"])
        self.assertFalse(session.trust_env)
        self.assertEqual(session.calls[0]["url"], "http://worker:5251/api/internal/rclone/status")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer secret")

    def test_client_fails_closed_without_token(self) -> None:
        session = _Session(_Response({"success": True}))
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="",
            database=object(),
            session=session,
        )

        result = client.start(reason="manual")
        self.assertFalse(result["success"])
        self.assertEqual(session.calls, [])

    def test_safe_status_request_retries_connection_and_gateway_failures(self) -> None:
        sleeps: list[float] = []
        session = _SequenceSession(
            [
                requests.ConnectionError("offline"),
                _Response({"success": False, "message": "restarting"}, 503),
                _Response({"success": True, "status": {"running": True}}),
            ]
        )
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
            sleep=sleeps.append,
        )

        self.assertTrue(client.status()["running"])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [0.2, 0.5])

    def test_side_effecting_start_is_not_retried(self) -> None:
        session = _SequenceSession([requests.ConnectionError("response lost")])
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
            sleep=lambda _delay: None,
        )

        result = client.start(reason="manual")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "worker_unavailable")
        self.assertEqual(result["_http_status"], 503)
        self.assertEqual(len(session.calls), 1)

    def test_timeout_maps_to_504(self) -> None:
        session = _SequenceSession([requests.Timeout("slow")])
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
        )

        result = client.stop()

        self.assertEqual(result["error_code"], "worker_timeout")
        self.assertEqual(result["_http_status"], 504)
        self.assertEqual(len(session.calls), 1)

    def test_webdav_http_error_is_raised_with_original_status(self) -> None:
        session = _SequenceSession(
            [_Response({"success": False, "message": "remote rejected"}, 409)]
        )
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
        )

        with self.assertRaises(RcloneWorkerRequestError) as raised:
            client.webdav_save({"remote_name": "MP"})

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(session.calls), 1)

    def test_idempotent_reload_retries_transient_http_status(self) -> None:
        session = _SequenceSession(
            [
                _Response({"success": False, "message": "restarting"}, 502),
                _Response({"success": True, "runtime_revision": 2}),
            ]
        )
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
            sleep=lambda _delay: None,
        )

        result = client.reload()

        self.assertTrue(result["success"])
        self.assertEqual(result["runtime_revision"], 2)
        self.assertEqual([call["method"] for call in session.calls], ["POST", "POST"])

    def test_log_failure_returns_worker_unavailable_contract(self) -> None:
        session = _SequenceSession(
            [requests.ConnectionError("offline") for _index in range(3)]
        )
        client = RcloneWorkerClient(
            base_url="http://worker:5251",
            token="secret",
            database=object(),
            session=session,
            sleep=lambda _delay: None,
        )

        result = client.get_logs(limit=100)

        self.assertIsInstance(result, dict)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "worker_unavailable")
        self.assertEqual(result["_http_status"], 503)
        self.assertEqual(len(session.calls), 3)


class LocalRcloneExecutionTests(unittest.TestCase):
    def test_environment_checker_builds_direct_rclone_commands(self) -> None:
        calls: list[list[str]] = []

        def runner(name: str, command: list[str], allow_empty: bool) -> dict[str, Any]:
            calls.append(command)
            return {"name": name, "ok": True, "message": "ok", "exit_code": 0}

        checker = RcloneEnvironmentChecker(
            {
                "execution_mode": "local",
                "remote_name": "MP",
                "config_path": "/config/rclone/rclone.conf",
                "cache_dir": "/cache",
            },
            command_runner=runner,
        )
        checker.check(Path(__file__), {name: "目录" for _, name in checker.DIRECTORY_LABELS})

        self.assertTrue(calls)
        self.assertTrue(all(command[0] == "rclone" for command in calls))
        self.assertTrue(all("docker" not in command for command in calls))
        self.assertTrue(any("--config" in command for command in calls[1:]))

    def test_cleanup_commands_are_direct_in_local_mode(self) -> None:
        service = object.__new__(RcloneService)
        service.config = {
            "execution_mode": "local",
            "config_path": "/config/rclone/rclone.conf",
            "cache_dir": "/cache",
        }

        self.assertEqual(service._cleanup_container_command("rm", "-f", "/temp/file"), ["rm", "-f", "/temp/file"])
        command = service._cleanup_rclone_command("deletefile", "MP:path/file")
        self.assertEqual(command[0], "rclone")
        self.assertNotIn("docker", command)
        self.assertIn("/config/rclone/rclone.conf", command)

    def test_webdav_service_runs_local_commands_without_docker(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({}), stderr="")

        service = RcloneWebdavConfigService(
            lambda: {"execution_mode": "local", "remote_name": "MP"},
            runner=runner,
        )
        result = service.status()

        self.assertFalse(result["configured"])
        self.assertEqual(commands[0][0], "rclone")
        self.assertNotIn("docker", commands[0])


class WorkerProxyContractTests(unittest.TestCase):
    def test_log_query_preserves_worker_unavailable_response(self) -> None:
        response = {
            "success": False,
            "status": "worker_unavailable",
            "error_code": "worker_unavailable",
            "message": "Worker 不可用",
            "_http_status": 503,
        }
        service = RcloneAdminQueryService(
            RcloneAdminQueryDependencies(
                rclone=SimpleNamespace(get_logs=lambda **_kwargs: response),
                counts=object(),
            )
        )

        self.assertEqual(service.logs(100), response)

    def test_file_retry_preserves_worker_transport_status(self) -> None:
        database = SimpleNamespace(
            get_rclone_file_event=lambda _event_id: {
                "id": 7,
                "status": "failed",
                "filename": "episode.mkv",
            },
            add_rclone_file_event=lambda **_kwargs: self.fail(
                "transport failure must not create a retry event"
            ),
            add_event=lambda *_args, **_kwargs: self.fail(
                "transport failure must not create a business event"
            ),
        )
        runner = SimpleNamespace(
            start_file_retry=lambda _event: {
                "success": False,
                "status": "worker_timeout",
                "error_code": "worker_timeout",
                "message": "Worker 超时",
                "_http_status": 504,
            }
        )
        service = RcloneFileRetryService(
            RcloneFileRetryDependencies(database=database, runner=runner)
        )

        result, status_code = service.retry(7, force=False)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "worker_timeout")
        self.assertEqual(status_code, 504)


if __name__ == "__main__":
    unittest.main()
