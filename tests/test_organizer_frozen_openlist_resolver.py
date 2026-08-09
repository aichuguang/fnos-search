from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import threading
import unittest

from fnos_media_import.organizer.service import (
    FrozenOpenListResolutionError,
    OrganizerService,
    build_frozen_openlist_client_resolver,
)


def _plan(
    *,
    job_id: int = 7,
    connection_id: str = "ol_main",
    connection_revision: int = 3,
    mount_id: str = "mount_media",
    mount_revision: int = 5,
) -> dict:
    namespace = f"openlist:{connection_id}:{mount_id}"
    identity = {
        "mount_id": mount_id,
        "mount_revision": mount_revision,
        "connection_id": connection_id,
        "connection_revision": connection_revision,
        "storage_identity_id": "provider:quark:account:acc_main",
        "identity_root": "/影视",
    }
    return {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": job_id,
        "category": "movie",
        "bindings": {
            "organizer_mount_id": mount_id,
            "organizer_mount_revision": mount_revision,
            "organizer_mount_namespace_id": namespace,
            "organizer_mount_storage_identity_id": identity["storage_identity_id"],
            "organizer_mount_identity_root": identity["identity_root"],
            "organizer_connection_id": connection_id,
            "organizer_connection_revision": connection_revision,
        },
        "paths": {
            "organizer": {
                "kind": "openlist",
                "namespace_id": namespace,
                "path": f"/_fnos_ingress/job-{job_id}",
                **identity,
            },
            "destination_final": {
                "kind": "native",
                "namespace_id": "native:mobile:media",
                "path": "/影视/电影",
            },
        },
        "postprocess": {"organizer_enabled": True},
    }


class _MountService(RepositoryFakeMixin):
    def __init__(self, revision: dict | None) -> None:
        self.revision = revision
        self.calls: list[tuple[str, int]] = []

    def get_revision(self, mount_id: str, revision: int):
        self.calls.append((mount_id, revision))
        return copy.deepcopy(self.revision)


class _ConnectionService(RepositoryFakeMixin):
    def __init__(self, revision: dict | None, credentials: dict | None) -> None:
        self.revision = revision
        self.credentials = credentials
        self.revision_calls: list[tuple[str, int]] = []
        self.credential_calls: list[tuple[str, int]] = []

    def get_revision(self, connection_id: str, revision: int):
        self.revision_calls.append((connection_id, revision))
        return copy.deepcopy(self.revision)

    def resolve_credentials(self, connection_id: str, revision: int):
        self.credential_calls.append((connection_id, revision))
        return copy.deepcopy(self.credentials)


class _Database(RepositoryFakeMixin):
    def __init__(self, tasks: dict[int, dict], jobs: dict[int, dict]) -> None:
        self.tasks = tasks
        self.jobs = jobs
        self.updates: list[dict] = []

    def get_organizer_task(self, task_id: int, **_kwargs):
        return self.tasks.get(task_id)

    def get_job(self, job_id: int):
        return self.jobs.get(job_id)

    def update_organizer_task(self, task_id: int, **updates):
        self.updates.append({"task_id": task_id, **updates})
        self.tasks[task_id].update(
            {
                key: updates[key]
                for key in ("status", "error_message", "raw_data")
                if key in updates
            }
        )
        return True


class _Client(RepositoryFakeMixin):
    configured = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.scan_calls = 0
        self.closed = 0

    def scan_videos(self, *_args, **_kwargs):
        self.scan_calls += 1
        return []

    def close(self) -> None:
        self.closed += 1


class FrozenOpenListResolverTests(unittest.TestCase):
    def _frozen_mount(self) -> dict:
        return {
            "mount_id": "mount_media",
            "revision": 5,
            "connection_id": "ol_main",
            "connection_revision": 3,
            "namespace_id": "openlist:ol_main:mount_media",
            "storage_identity_id": "provider:quark:account:acc_main",
            "identity_root": "/影视",
            "enabled": True,
            "readable": True,
            "writable": True,
            "identity_status": "verified",
        }

    def _frozen_connection(self) -> dict:
        return {
            "connection_id": "ol_main",
            "revision": 3,
            "base_url": "http://frozen-openlist:5244/dav",
            "username": "frozen-user",
            "enabled": True,
        }

    def _frozen_credentials(self) -> dict:
        return {
            "connection_id": "ol_main",
            "revision": 3,
            "base_url": "http://frozen-openlist:5244/dav",
            "username": "frozen-user",
            "password": "frozen-password",
            "enabled": True,
        }

    def test_factory_uses_frozen_revisions_and_only_runtime_tuning(self) -> None:
        mount_service = _MountService(self._frozen_mount())
        connection_service = _ConnectionService(
            self._frozen_connection(),
            self._frozen_credentials(),
        )
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
            {
                "base_url": "http://current-openlist:9999",
                "username": "current-user",
                "password": "current-password",
                "token": "current-token",
                "timeout": 17,
                "request_retries": 4,
            },
        )

        client = resolver({"id": 7}, _plan())
        try:
            self.assertEqual(client.base_url, "http://frozen-openlist:5244")
            self.assertEqual(client.username, "frozen-user")
            self.assertEqual(client.password, "frozen-password")
            self.assertEqual(client.token, "")
            self.assertEqual(client.timeout, 17)
            self.assertEqual(client.request_retries, 4)
            self.assertEqual(mount_service.calls, [("mount_media", 5)])
            self.assertEqual(connection_service.revision_calls, [("ol_main", 3)])
            self.assertEqual(connection_service.credential_calls, [("ol_main", 3)])
            self.assertEqual(client._fnos_frozen_identity["mount_revision"], 5)
            self.assertEqual(client._fnos_frozen_identity["connection_revision"], 3)
        finally:
            client.close()

    def test_namespace_mismatch_is_rejected_before_revision_lookup(self) -> None:
        mount_service = _MountService(self._frozen_mount())
        connection_service = _ConnectionService(
            self._frozen_connection(),
            self._frozen_credentials(),
        )
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )
        plan = _plan()
        plan["paths"]["organizer"]["namespace_id"] = "openlist:other:mount_media"

        with self.assertRaisesRegex(FrozenOpenListResolutionError, "namespace"):
            resolver({"id": 7}, plan)
        self.assertEqual(mount_service.calls, [])
        self.assertEqual(connection_service.revision_calls, [])
        self.assertEqual(connection_service.credential_calls, [])

    def test_destination_alias_namespace_does_not_override_organizer_binding(self) -> None:
        mount_service = _MountService(self._frozen_mount())
        connection_service = _ConnectionService(
            self._frozen_connection(),
            self._frozen_credentials(),
        )
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )
        plan = _plan()
        plan["paths"]["destination_final"] = {
            "kind": "openlist",
            "namespace_id": "openlist:alias:destination_mount",
            "path": "/影视/电影",
            "mount_id": "destination_mount",
            "mount_revision": 12,
            "connection_id": "alias",
            "connection_revision": 6,
            "storage_identity_id": "provider:quark:account:acc_main",
            "identity_root": "/影视",
        }

        client = resolver({"id": 7}, plan)
        try:
            self.assertEqual(client._fnos_frozen_identity["namespace_id"], "openlist:ol_main:mount_media")
            self.assertEqual(mount_service.calls, [("mount_media", 5)])
            self.assertEqual(connection_service.credential_calls, [("ol_main", 3)])
        finally:
            client.close()

    def test_missing_mount_revision_is_rejected_without_connection_lookup(self) -> None:
        mount_service = _MountService(None)
        connection_service = _ConnectionService(
            self._frozen_connection(),
            self._frozen_credentials(),
        )
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )

        with self.assertRaisesRegex(FrozenOpenListResolutionError, "mount 修订不存在"):
            resolver({"id": 7}, _plan())
        self.assertEqual(connection_service.revision_calls, [])
        self.assertEqual(connection_service.credential_calls, [])

    def test_missing_connection_revision_is_rejected_without_credentials(self) -> None:
        mount_service = _MountService(self._frozen_mount())
        connection_service = _ConnectionService(None, self._frozen_credentials())
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )

        with self.assertRaisesRegex(FrozenOpenListResolutionError, "connection 修订不存在"):
            resolver({"id": 7}, _plan())
        self.assertEqual(connection_service.credential_calls, [])

    def test_credentials_must_match_frozen_connection_revision(self) -> None:
        credentials = self._frozen_credentials()
        credentials["base_url"] = "http://current-openlist:6244/dav"
        mount_service = _MountService(self._frozen_mount())
        connection_service = _ConnectionService(self._frozen_connection(), credentials)
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )

        with self.assertRaisesRegex(FrozenOpenListResolutionError, "credentials"):
            resolver({"id": 7}, _plan())

    def test_mount_connection_revision_mismatch_is_rejected(self) -> None:
        frozen_mount = self._frozen_mount()
        frozen_mount["connection_revision"] = 9
        mount_service = _MountService(frozen_mount)
        connection_service = _ConnectionService(
            self._frozen_connection(),
            self._frozen_credentials(),
        )
        resolver = build_frozen_openlist_client_resolver(
            mount_service,
            connection_service,
        )

        with self.assertRaisesRegex(FrozenOpenListResolutionError, "mount 修订"):
            resolver({"id": 7}, _plan())
        self.assertEqual(connection_service.revision_calls, [])
        self.assertEqual(connection_service.credential_calls, [])

    def test_missing_resolver_moves_task_to_review_without_default_client(self) -> None:
        plan = _plan()
        task = {
            "id": 1,
            "job_id": 7,
            "category": "movie",
            "status": "pending",
            "revision": 1,
            "openlist_root_path": plan["paths"]["organizer"]["path"],
            "raw_data": {"execution_plan": plan},
        }
        job = {"id": 7, "status": "waiting", "raw_data": {"execution_plan": plan}}
        service = OrganizerService.__new__(OrganizerService)
        service.db = _Database({1: task}, {7: job})
        default = _Client("default")
        service.openlist = default
        service._openlist_context = threading.local()
        service._openlist_client_resolver = None
        # Isolate this test from the broad V1 envelope validator; this case is
        # specifically about the execution client guard after plan validation.
        service._task_v1_plan = lambda *_args, **_kwargs: (job, plan, None)

        result = service.process_task(1)

        self.assertTrue(result.get("manual_review"))
        self.assertEqual(task["status"], "waiting_review")
        self.assertEqual(default.scan_calls, 0)

    def test_thread_local_scopes_do_not_leak_between_connections(self) -> None:
        first_plan = _plan(job_id=7, connection_id="ol_a", mount_id="mount_a")
        second_plan = _plan(job_id=8, connection_id="ol_b", mount_id="mount_b")
        tasks = {
            1: {"id": 1, "job_id": 7},
            2: {"id": 2, "job_id": 8},
        }
        jobs = {7: {"id": 7}, 8: {"id": 8}}
        service = OrganizerService.__new__(OrganizerService)
        service.db = _Database(tasks, jobs)
        default = _Client("default")
        service.openlist = default
        service._openlist_context = threading.local()
        created: dict[int, _Client] = {}

        def resolver(job: dict, _frozen_plan: dict) -> _Client:
            client = _Client(f"job-{job['id']}")
            created[int(job["id"])] = client
            return client

        service.set_openlist_client_resolver(resolver)
        barrier = threading.Barrier(2)
        seen: dict[int, tuple[str, str]] = {}

        def run(task_id: int, plan: dict) -> None:
            task = tasks[task_id]
            with service._task_openlist_scope(task, plan, context="test"):
                first = service.openlist.name
                barrier.wait(timeout=5)
                second = service.openlist.name
                seen[task_id] = (first, second)

        threads = [
            threading.Thread(target=run, args=(1, first_plan)),
            threading.Thread(target=run, args=(2, second_plan)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(seen[1], ("job-7", "job-7"))
        self.assertEqual(seen[2], ("job-8", "job-8"))
        self.assertIs(service.openlist, default)
        self.assertIsNot(created[7], created[8])
        self.assertEqual(created[7].closed, 1)
        self.assertEqual(created[8].closed, 1)
        self.assertEqual(default.closed, 0)

    def test_nested_same_task_scope_reuses_and_closes_once(self) -> None:
        plan = _plan()
        task = {"id": 1, "job_id": 7}
        service = OrganizerService.__new__(OrganizerService)
        service.db = _Database({1: task}, {7: {"id": 7}})
        default = _Client("default")
        service.openlist = default
        service._openlist_context = threading.local()
        calls = 0
        scoped = _Client("scoped")

        def resolver(_job: dict, _frozen_plan: dict) -> _Client:
            nonlocal calls
            calls += 1
            return scoped

        service.set_openlist_client_resolver(resolver)
        with service._task_openlist_scope(task, plan, context="outer") as outer:
            with service._task_openlist_scope(task, plan, context="inner") as inner:
                self.assertIs(outer, inner)
                self.assertIs(service.openlist, scoped)
            self.assertEqual(scoped.closed, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(scoped.closed, 1)
        self.assertIs(service.openlist, default)


if __name__ == "__main__":
    unittest.main()
