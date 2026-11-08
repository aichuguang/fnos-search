from __future__ import annotations

import posixpath
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.database import Database
from fnos_media_import.organizer.identity import IDENTITY_MANIFEST_NAME, parse_identity_manifest, serialize_identity_manifest
from fnos_media_import.organizer.openlist_client import OpenListItem
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.repositories import RepositorySet
from tests._organizer_v1_fixtures import attach_organizer_v1_task, bind_task_openlist_resolver, organizer_v1_job, organizer_v1_plan
from tests._repository_v1_fakes import RepositoryFakeMixin


LIBRARY_ROOT = "/library/tv"
CANONICAL_ROOT = LIBRARY_ROOT + "/Example (2026) [tmdb-123]"
MEDIA_IDENTITY = {"media_type": "tv", "ns": "tmdb", "value": "123", "tmdb_id": 123, "title": "Example", "year": 2026}
TMDB_ITEM = {"id": 123, "title": "Example", "original_title": "Example", "year": "2026", "media_type": "tv"}


class _PlanRepository(RepositoryFakeMixin):
    def __init__(self, job):
        self.job = job
        self.matches = []

    def get_job(self, job_id):
        return self.job if self.job["id"] == job_id else None

    def add_organizer_tmdb_match(self, *args):
        self.matches.append(args)


class _IdentityCloud:
    """In-memory OpenList surface; every mutating call is observable."""

    configured = True

    def __init__(self):
        self.dirs = {"/", "/library", LIBRARY_ROOT}
        self.files = {}
        self.manifests = {}
        self.mutations = []
        self.reads = []
        self.fail_manifest_write = False
        self.fail_read_after_manifest_write = False
        self._manifest_read_failure_pending = False
        self.hide_final_videos_on_list = False
        self.hidden_paths = set()

    def seed_dir(self, path):
        while path:
            self.dirs.add(path)
            if path == "/":
                break
            path = posixpath.dirname(path)

    def seed_video(self, path, size=100_000_000):
        self.seed_dir(posixpath.dirname(path))
        item = OpenListItem(name=posixpath.basename(path), path=path, is_dir=False, size=size, raw={})
        self.files[path] = item
        return item

    def seed_identity(self, context):
        root = context["canonical_dir"]
        self.seed_dir(root)
        self.manifests[root + "/" + IDENTITY_MANIFEST_NAME] = serialize_identity_manifest(parse_identity_manifest(context["manifest"]))

    def list_dir(self, path, **kwargs):
        self.reads.append(("list_dir", path, kwargs))
        if path not in self.dirs:
            raise RuntimeError("Directory not found: " + path)
        directories = [OpenListItem(name=posixpath.basename(child), path=child, is_dir=True, raw={})
                       for child in sorted(self.dirs) if child != "/" and posixpath.dirname(child) == path]
        if self.hide_final_videos_on_list and path.startswith(CANONICAL_ROOT + "/Season "):
            self.hidden_paths.update(item.path for item in self.files.values() if posixpath.dirname(item.path) == path)
        files = [item for item in self.files.values() if posixpath.dirname(item.path) == path and item.path not in self.hidden_paths]
        manifests = [OpenListItem(name=IDENTITY_MANIFEST_NAME, path=child, is_dir=False, raw={})
                     for child in self.manifests if posixpath.dirname(child) == path]
        return directories + files + manifests

    def exists(self, path):
        if path in self.hidden_paths:
            return False
        return path in self.dirs or path in self.files or path in self.manifests

    def get_item(self, path):
        if path in self.hidden_paths:
            return None
        if path in self.dirs:
            return OpenListItem(name=posixpath.basename(path), path=path, is_dir=True, raw={})
        return self.files.get(path)

    def read_identity_manifest(self, path):
        self.reads.append(("read_identity_manifest", path))
        if self._manifest_read_failure_pending:
            self._manifest_read_failure_pending = False
            raise RuntimeError("Simulated post-write manifest read failure")
        return self.manifests.get(path)

    def write_identity_manifest(self, path, content):
        self.mutations.append(("write_identity_manifest", path))
        if self.fail_manifest_write:
            return False
        self.manifests[path] = content
        if self.fail_read_after_manifest_write:
            self.fail_read_after_manifest_write = False
            self._manifest_read_failure_pending = True
        return True

    def mkdir(self, path):
        self.mutations.append(("mkdir", path))
        self.seed_dir(path)
        return True

    def remove_file(self, path):
        self.mutations.append(("remove_file", path))
        self.files.pop(path, None)
        self.manifests.pop(path, None)
        return True

    def remove_empty_directory(self, path):
        if self.list_dir(path):
            raise RuntimeError("Refusing to remove a nonempty directory")
        self.mutations.append(("remove_empty_directory", path))
        self.dirs.discard(path)
        return True

    def rename(self, path, name, **kwargs):
        self.mutations.append(("rename", path, name))
        item = self.files.pop(path)
        self.seed_video(posixpath.join(posixpath.dirname(path), name), item.size)
        return True

    def move(self, path, target_dir, **kwargs):
        self.mutations.append(("move", path, target_dir))
        item = self.files.pop(path)
        self.seed_video(posixpath.join(target_dir, item.name), item.size)
        return True

    def move_many(self, source_dir, target_dir, names, **kwargs):
        for name in names:
            self.move(posixpath.join(source_dir, name), target_dir, **kwargs)
        return True


def _plan_service(cloud, *, job_id=1, title="Example (2026)", intent="create_or_match"):
    plan = organizer_v1_plan(job_id, category="tv", organizer_root=f"/incoming/job-{job_id}", destination_final=LIBRARY_ROOT)
    job = organizer_v1_job(plan, status="waiting_organizer", raw_data={"request": {
        "media_identity": dict(MEDIA_IDENTITY), "organizer_intent": intent,
    }})
    task = attach_organizer_v1_task({"id": job_id, "title": title, "source_keyword": title, "raw_data": {}}, plan)
    service = OrganizerService.__new__(OrganizerService)
    service.db = _PlanRepository(job)
    service.organizer_config = {"auto_apply_confidence": 85}
    service.categories = {"tv": {"label": "TV"}}
    service.ai = SimpleNamespace(configured=False)
    service.tmdb = SimpleNamespace(
        configured=True,
        search=lambda *_args, **_kwargs: [dict(TMDB_ITEM)],
        details=lambda *_args, **_kwargs: dict(TMDB_ITEM),
    )
    service.openlist = cloud
    bind_task_openlist_resolver(service, cloud, plan)
    return service, task


def _build(service, task, cloud, relative_names, *, size=100_000_000):
    videos = [cloud.seed_video(task["openlist_root_path"] + "/" + name, size) for name in relative_names]
    return service._build_plan(task, "tv", service.categories["tv"], videos)


class OrganizerIdentityServiceTests(unittest.TestCase):
    def setUp(self):
        self.cloud = _IdentityCloud()

    def test_new_weak_local_identity_requires_confirmation_without_writes(self):
        service, task = _plan_service(self.cloud)
        service.db.job["raw_data"]["request"].pop("media_identity")
        service.tmdb = SimpleNamespace(configured=False, search=lambda *_args, **_kwargs: [])
        _files, mappings, _ops, summary = _build(service, task, self.cloud, ["Example.S01E01.mkv"])
        self.assertEqual(summary["evidence"]["organizer_identity"]["manifest"]["identity"]["ns"], "local")
        self.assertEqual(mappings[0]["status"], "ready")
        self.assertFalse(summary["auto_applicable"])
        self.assertEqual(self.cloud.mutations, [])

    def test_first_import_plan_is_read_only_and_targets_tagged_directory(self):
        service, task = _plan_service(self.cloud)
        _files, mappings, operations, summary = _build(service, task, self.cloud, ["Example.S01E01.mkv"])
        self.assertEqual(self.cloud.mutations, [])
        self.assertFalse(self.cloud.exists(CANONICAL_ROOT))
        self.assertEqual(mappings[0]["target_path"], CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv")
        self.assertEqual(mappings[0]["status"], "ready", mappings[0]["reason"])
        self.assertTrue(summary["auto_applicable"], summary)
        kinds = [operation["type"] for operation in operations]
        self.assertIn("write_identity_manifest", kinds)
        self.assertLess(kinds.index("write_identity_manifest"), kinds.index("move_file"))

    def test_same_tmdb_reuses_directory_despite_task_title_variation(self):
        first_service, first_task = _plan_service(self.cloud)
        _files, _mappings, _ops, first = _build(first_service, first_task, self.cloud, ["Example.S01E01.mkv"])
        self.cloud.seed_identity(first["evidence"]["organizer_identity"])
        service, task = _plan_service(self.cloud, job_id=2, title="Example 2026 1080P", intent="update_existing")
        _files, mappings, operations, second = _build(service, task, self.cloud, ["Example.S01E02.mkv"])
        self.assertEqual(second["evidence"]["organizer_identity"]["canonical_dir"], CANONICAL_ROOT)
        self.assertFalse(second["evidence"]["organizer_identity"]["is_new"])
        self.assertEqual(mappings[0]["status"], "ready", mappings[0]["reason"])
        self.assertEqual(mappings[0]["target_path"], CANONICAL_ROOT + "/Season 01/Example (2026) - S01E02.mkv")
        self.assertFalse(any(op["type"] == "create_dir" and op["target_path"] == CANONICAL_ROOT for op in operations))
        self.assertEqual(self.cloud.mutations, [])

    def test_update_without_identity_directory_is_blocked_without_writes(self):
        service, task = _plan_service(self.cloud, intent="update_existing")
        _files, mappings, operations, summary = _build(service, task, self.cloud, ["Example.S01E02.mkv"])
        self.assertEqual(mappings[0]["status"], "need_edit")
        self.assertFalse(summary["auto_applicable"])
        self.assertTrue(summary["evidence"]["organizer_identity"]["errors"])
        self.assertEqual(operations, [])
        self.assertEqual(self.cloud.mutations, [])

    def test_update_may_create_a_new_season_but_not_a_new_work_root(self):
        first_service, first_task = _plan_service(self.cloud)
        _files, _mappings, _ops, first = _build(first_service, first_task, self.cloud, ["Example.S01E01.mkv"])
        self.cloud.seed_identity(first["evidence"]["organizer_identity"])
        service, task = _plan_service(self.cloud, job_id=2, intent="update_existing")
        _files, mappings, operations, _summary = _build(service, task, self.cloud, ["Example.S02E01.mkv"])
        self.assertEqual(mappings[0]["status"], "ready", mappings[0]["reason"])
        created = [op["target_path"] for op in operations if op["type"] == "create_dir"]
        self.assertIn(CANONICAL_ROOT + "/Season 02", created)
        self.assertNotIn(CANONICAL_ROOT, created)
        self.assertEqual(self.cloud.mutations, [])

    def test_unparseable_small_video_is_preserved_instead_of_deleted(self):
        service, task = _plan_service(self.cloud)
        _files, mappings, operations, summary = _build(service, task, self.cloud, ["x264-10bit.mp4"], size=1024)
        self.assertEqual(mappings[0]["status"], "need_edit")
        self.assertEqual(summary["auto_delete_count"], 0)
        self.assertFalse(any("delete" in operation["type"] for operation in operations))
        self.assertTrue(self.cloud.exists(task["openlist_root_path"] + "/x264-10bit.mp4"))
        self.assertEqual(self.cloud.mutations, [])

    def test_container_source_does_not_drop_valid_video_or_leak_into_target(self):
        service, task = _plan_service(self.cloud)
        source = "mobile share 1234567890abcdef/Example.S01E03.mkv"
        _files, mappings, operations, _summary = _build(service, task, self.cloud, [source])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["status"], "ready", mappings[0]["reason"])
        self.assertNotIn("mobile share", mappings[0]["target_path"])
        self.assertTrue(any(op["type"] == "move_file" for op in operations))
        self.assertEqual(self.cloud.mutations, [])

    def test_obviously_different_source_title_requires_review(self):
        service, task = _plan_service(self.cloud)
        _files, mappings, operations, summary = _build(service, task, self.cloud, ["NCIS.S01E01.mkv"])
        self.assertEqual(mappings[0]["status"], "need_edit", mappings[0]["reason"])
        self.assertFalse(summary["auto_applicable"])
        self.assertEqual(operations, [])
        self.assertEqual(self.cloud.mutations, [])

    def test_quality_conflict_resolution_cannot_clear_title_review(self):
        first_service, first_task = _plan_service(self.cloud)
        _files, _mappings, _ops, first = _build(first_service, first_task, self.cloud, ["Example.S01E01.mkv"])
        self.cloud.seed_identity(first["evidence"]["organizer_identity"])
        self.cloud.seed_video(CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv")
        service, task = _plan_service(self.cloud, job_id=2, intent="update_existing")
        _files, mappings, operations, summary = _build(service, task, self.cloud, ["NCIS.S01E01.2160p.mkv"])
        self.assertEqual(mappings[0]["status"], "need_edit", mappings[0]["reason"])
        self.assertFalse(summary["auto_applicable"])
        self.assertEqual(operations, [])
        self.assertEqual(self.cloud.mutations, [])


class OrganizerIdentityApplyTests(unittest.TestCase):
    """Real SQLite plan, approval, lease, operation journal and confirmation."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="organizer-identity-apply-")
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "test.db")
        self.database.init_schema()
        self.repo = RepositorySet.from_database(self.database)
        self.cloud = _IdentityCloud()
        self.serial = 0

    def create_task(self, name="Example.S01E01.mkv", *, intent="create_or_match", subscription_id=None):
        self.serial += 1
        job_id, _created = self.repo.job_commands.create({
            "title": "Example (2026)", "category": "tv", "category_label": "TV", "status": "waiting_organizer",
            "source_provider_kind": "quark", "source_form": "share", "source_url": "source-record:test",
            "destination_id": "native-main", "plan_version": 1, "idempotency_key": f"identity-apply-{self.serial}",
        })
        service, task_fixture = _plan_service(self.cloud, job_id=job_id, intent=intent)
        raw = service.db.job["raw_data"]
        if subscription_id:
            raw["request"]["update_subscription_id"] = subscription_id
        self.repo.job_commands.update(job_id, raw_data=raw)
        service.db = self.repo
        service.owner_id = "identity-lifecycle-test"
        # The only external completion services are disabled; identity, paths,
        # approval, leases, operations and remote confirmation remain real.
        service._refresh_openlist_strm_for_task = lambda *_args: {}
        service._refresh_fnos_if_needed = lambda *_args: False
        task_id = self.repo.organizer.create_organizer_task(
            job_id=job_id, category="tv", title="Example (2026)", source_keyword="Example (2026)",
            openlist_root_path=task_fixture["openlist_root_path"], status="waiting_review", raw_data=task_fixture["raw_data"],
        )
        task = self.repo.organizer.get_organizer_task(task_id)
        files, mappings, operations, summary = _build(service, task, self.cloud, name if isinstance(name, list) else [name])
        self.assertTrue(summary["auto_applicable"], summary)
        self.assertTrue(self.repo.organizer.replace_organizer_plan(task_id, files=files, mappings=mappings, operations=operations))
        self.repo.organizer.update_organizer_task(task_id, confidence=summary["confidence"], evidence=summary["evidence"])
        approval = service.approve_task(task_id)
        self.assertTrue(approval["success"], approval)
        self.assertEqual(self.cloud.mutations, [])
        return service, job_id, task_id

    def test_first_apply_creates_identity_before_moving_and_finishes_journal(self):
        service, job_id, task_id = self.create_task()
        result = service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        target = CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv"
        self.assertTrue(self.cloud.exists(target))
        self.assertFalse(self.cloud.exists(f"/incoming/job-{job_id}/Example.S01E01.mkv"))
        mutations = [item[0] for item in self.cloud.mutations]
        self.assertLess(mutations.index("write_identity_manifest"), mutations.index("move"))
        self.assertEqual(self.cloud.mutations.count(("mkdir", CANONICAL_ROOT)), 1)
        task = self.repo.organizer.get_organizer_task(task_id)
        self.assertEqual(task["status"], "done")
        self.assertTrue(all(op["status"] in {"done", "skipped"} for op in task["operations"]))

    def test_manifest_failure_stops_before_any_video_rename_or_move(self):
        service, job_id, task_id = self.create_task()
        self.cloud.fail_manifest_write = True
        result = service.apply_task_from_worker(task_id)
        self.assertFalse(result["success"])
        self.assertFalse(any(item[0] in {"move", "rename"} for item in self.cloud.mutations))
        self.assertTrue(self.cloud.exists(f"/incoming/job-{job_id}/Example.S01E01.mkv"))
        task = self.repo.organizer.get_organizer_task(task_id)
        self.assertEqual(next(op["status"] for op in task["operations"] if op["type"] == "write_identity_manifest"), "failed")
        self.assertEqual(next(op["status"] for op in task["operations"] if op["type"] == "move_file"), "pending")

    def test_first_import_rollback_restores_video_and_removes_owned_manifest_and_empty_root(self):
        service, job_id, task_id = self.create_task()
        source = f"/incoming/job-{job_id}/Example.S01E01.mkv"
        target = CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv"
        manifest_path = CANONICAL_ROOT + "/" + IDENTITY_MANIFEST_NAME
        applied = service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertTrue(self.cloud.exists(target))
        self.assertTrue(self.cloud.exists(manifest_path))
        self.assertFalse(self.cloud.exists(source))
        rolled_back = service.rollback_run(applied["run_id"])
        self.assertTrue(rolled_back["success"], rolled_back)
        self.assertTrue(self.cloud.exists(source))
        self.assertFalse(self.cloud.exists(target))
        self.assertFalse(self.cloud.exists(manifest_path))
        self.assertFalse(self.cloud.exists(CANONICAL_ROOT))
        self.assertFalse(any(".organizer-rollback-" in path for path in self.cloud.dirs))
        source_run = self.repo.organizer.get_organizer_run(applied["run_id"])
        self.assertEqual(source_run["summary"]["rollback"]["status"], "completed")
        mutations_before_retry = list(self.cloud.mutations)
        repeated = service.rollback_run(applied["run_id"])
        self.assertTrue(repeated["success"], repeated)
        self.assertTrue(repeated["already_rolled_back"])
        self.assertEqual(self.cloud.mutations, mutations_before_retry)

    def test_retry_after_manifest_write_ack_loss_reuses_manifest_and_root(self):
        service, _job_id, task_id = self.create_task()
        self.cloud.fail_read_after_manifest_write = True
        first = service.apply_task_from_worker(task_id)
        self.assertFalse(first["success"])
        self.assertTrue(self.cloud.read_identity_manifest(CANONICAL_ROOT + "/" + IDENTITY_MANIFEST_NAME))
        self.assertFalse(any(item[0] == "move" for item in self.cloud.mutations))
        approved = service.approve_task(task_id)
        self.assertTrue(approved["success"], approved)
        retried = service.apply_task_from_worker(task_id)
        self.assertTrue(retried["success"], retried)
        self.assertEqual(self.cloud.mutations.count(("mkdir", CANONICAL_ROOT)), 1)
        self.assertEqual(len([item for item in self.cloud.mutations if item[0] == "write_identity_manifest"]), 1)
        self.assertEqual(len([item for item in self.cloud.mutations if item[0] == "move"]), 1)

    def test_new_season_apply_does_not_recreate_existing_work_root(self):
        first, _job_id, task_id = self.create_task()
        self.assertTrue(first.apply_task_from_worker(task_id)["success"])
        self.cloud.mutations.clear()
        service, _job_id, task_id = self.create_task("Example.S02E01.mkv", intent="update_existing")
        result = service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        self.assertIn(("mkdir", CANONICAL_ROOT + "/Season 02"), self.cloud.mutations)
        self.assertNotIn(("mkdir", CANONICAL_ROOT), self.cloud.mutations)
        self.assertTrue(self.cloud.exists(CANONICAL_ROOT + "/Season 02/Example (2026) - S02E01.mkv"))

    def test_subscription_is_bound_only_after_actual_target_confirmation(self):
        subscription_id = self.repo.update.create_update_subscription({"title": "Example", "category": "tv", "media_type": "tv", "tmdb_id": 123})
        service, job_id, task_id = self.create_task(subscription_id=subscription_id)
        events = []
        confirm = service._confirm_standardized_targets
        bind = self.repo.update.bind_organized_subscription
        def observed_confirm(task):
            result = confirm(task)
            events.append(("confirmed", result["success"]))
            return result
        def observed_bind(*args, **kwargs):
            self.assertEqual(events, [("confirmed", True)])
            events.append(("bound", args[0]))
            return bind(*args, **kwargs)
        with patch.object(service, "_confirm_standardized_targets", side_effect=observed_confirm), patch.object(self.repo.update, "bind_organized_subscription", side_effect=observed_bind):
            result = service.apply_task_from_worker(task_id)
        self.assertTrue(result["success"], result)
        self.assertEqual(events, [("confirmed", True), ("bound", job_id)])
        self.assertEqual(self.repo.update.get_update_subscription(subscription_id)["canonical_dir"], CANONICAL_ROOT)

    def test_failed_real_confirmation_does_not_bind_subscription(self):
        subscription_id = self.repo.update.create_update_subscription({"title": "Example", "category": "tv", "media_type": "tv", "tmdb_id": 123})
        service, _job_id, task_id = self.create_task(subscription_id=subscription_id)
        # Simulate remote visibility disappearing at the refreshed final-folder
        # scan, after the move itself has successfully verified its destination.
        self.cloud.hide_final_videos_on_list = True
        with patch.object(self.repo.update, "bind_organized_subscription", wraps=self.repo.update.bind_organized_subscription) as bind:
            result = service.apply_task_from_worker(task_id)
        self.assertFalse(result["success"], result)
        bind.assert_not_called()
        self.assertIsNone(self.repo.update.get_update_subscription(subscription_id)["canonical_dir"])
        self.assertTrue(any(item[0] == "move" for item in self.cloud.mutations))

    def test_manual_title_then_season_edit_preserves_strong_identity_and_applies(self):
        service, _job_id, task_id = self.create_task()
        mapping = self.repo.organizer.get_organizer_task(task_id)["mappings"][0]
        changed = service.update_mapping(mapping["id"], {"title": "EXAMPLE"}, task_id=task_id)
        self.assertTrue(changed["success"], changed)
        after_title = self.repo.organizer.get_organizer_task(task_id)["mappings"][0]
        context = after_title["raw_data"]["organizer_identity"]
        self.assertEqual(context["canonical_dir"], CANONICAL_ROOT)
        self.assertEqual(parse_identity_manifest(context["manifest"]).identity.key, ("tv", "tmdb", "123"))
        changed = service.update_mapping(mapping["id"], {"season": 2}, task_id=task_id)
        self.assertTrue(changed["success"], changed)
        edited = self.repo.organizer.get_organizer_task(task_id)["mappings"][0]
        self.assertEqual(edited["raw_data"]["organizer_identity"]["canonical_dir"], CANONICAL_ROOT)
        self.assertTrue(edited["raw_data"]["manual_episode_override"])
        self.assertEqual(self.cloud.mutations, [])
        self.assertTrue(service.approve_task(task_id)["success"])
        applied = service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertTrue(self.cloud.exists(CANONICAL_ROOT + "/Season 02/Example (2026) - S02E01.mkv"))

    def test_manual_single_tmdb_change_recalculates_identity_and_applies(self):
        service, _job_id, task_id = self.create_task()
        service.tmdb.details = lambda *_args, **_kwargs: {**TMDB_ITEM, "id": 456, "title": "Corrected Example", "original_title": "Corrected Example"}
        mapping = self.repo.organizer.get_organizer_task(task_id)["mappings"][0]
        changed = service.update_mapping(mapping["id"], {"title": "Corrected Example", "tmdb_id": 456}, task_id=task_id)
        self.assertTrue(changed["success"], changed)
        edited = self.repo.organizer.get_organizer_task(task_id)["mappings"][0]
        context = edited["raw_data"]["organizer_identity"]
        canonical = LIBRARY_ROOT + "/Corrected Example (2026) [tmdb-456]"
        self.assertEqual(edited["tmdb_id"], 456)
        self.assertEqual(context["canonical_dir"], canonical)
        self.assertEqual(parse_identity_manifest(context["manifest"]).identity.key, ("tv", "tmdb", "456"))
        self.assertEqual(self.cloud.mutations, [])
        self.assertTrue(service.approve_task(task_id)["success"])
        applied = service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertTrue(self.cloud.exists(edited["target_path"]))
        self.assertFalse(self.cloud.exists(CANONICAL_ROOT))

    def test_manual_batch_tmdb_change_keeps_identity_for_all_mappings(self):
        service, _job_id, task_id = self.create_task(["Example.S01E01.mkv", "Example.S01E02.mkv"])
        service.tmdb.details = lambda *_args, **_kwargs: {**TMDB_ITEM, "id": 456, "title": "Corrected Example", "original_title": "Corrected Example"}
        changed = service.batch_update_mappings(task_id, {"title": "Corrected Example", "tmdb_id": 456})
        self.assertTrue(changed["success"], changed)
        mappings = self.repo.organizer.get_organizer_task(task_id)["mappings"]
        self.assertEqual(len(mappings), 2)
        for mapping in mappings:
            self.assertEqual(mapping["tmdb_id"], 456)
            context = mapping["raw_data"]["organizer_identity"]
            self.assertEqual(parse_identity_manifest(context["manifest"]).identity.key, ("tv", "tmdb", "456"))
            self.assertEqual(context["canonical_dir"], LIBRARY_ROOT + "/Corrected Example (2026) [tmdb-456]")
        self.assertEqual(self.cloud.mutations, [])
        self.assertTrue(service.approve_task(task_id)["success"])
        applied = service.apply_task_from_worker(task_id)
        self.assertTrue(applied["success"], applied)
        self.assertTrue(all(self.cloud.exists(mapping["target_path"]) for mapping in mappings))



if __name__ == "__main__":
    unittest.main()
