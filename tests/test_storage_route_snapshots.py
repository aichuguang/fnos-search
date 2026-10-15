from __future__ import annotations

import copy
import json
import shutil
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.storage.assignment_service import AssignmentConflictError, SourceAssignmentService
from fnos_media_import.storage.blueprint import StorageBlueprintContext, create_storage_blueprint
from fnos_media_import.storage.setup_blueprint import StorageSetupBlueprintContext, create_storage_setup_blueprint
from tests import test_storage_setup_v1 as fixtures


ROOT = Path(__file__).resolve().parents[1]


class StorageRouteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StorageSetupV1Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.assignments = self.fixture.assignments
        self.a = self.service.save_location({"kind": "local", "name": "First"})["destination_id"]
        self.b = self.service.save_location({"kind": "local", "name": "Second"})["destination_id"]
        app = Flask(__name__)
        app.register_blueprint(create_storage_setup_blueprint(StorageSetupBlueprintContext(
            admin_required=lambda handler: handler, service=self.service,
        )))
        app.register_blueprint(create_storage_blueprint(StorageBlueprintContext(
            admin_required=lambda handler: handler, target_service=self.fixture.targets,
            assignment_service=self.assignments,
        )))
        self.client = app.test_client()

    def _snapshot(self):
        response = self.client.get("/api/admin/storage-setup")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    @staticmethod
    def _payload(snapshot):
        return {"routing_token": snapshot["routing_token"], "routes": [
            {"search_source_id": row["search_source_id"], "default_destination_id": row["default_destination_id"] or "",
             "overrides": dict(row["overrides"]), "assignment_revisions": dict(row["assignment_revisions"])}
            for row in snapshot["routes"]
        ]}

    def _put(self, payload):
        return self.client.put("/api/admin/storage-setup/routes", json=payload)

    def _route(self, payload, source="quark"):
        return next(row for row in payload["routes"] if row["search_source_id"] == source)

    def _configure(self, *, default=None, overrides=None):
        # Explicit internal setup remains backward compatible; HTTP writes are
        # independently required to send a complete routing token.
        self.service.save_routes({"routes": [{"search_source_id": "quark", "default_destination_id": default or "",
                                             "overrides": overrides or {}}]})

    def _state(self):
        return {(row.search_source_id, row.category_key): (row.destination_id, row.revision)
                for row in self.assignments.snapshot()["assignments"]}

    def test_partial_same_target_round_trip_preserves_unassigned_categories(self):
        self._configure(overrides={"movie": self.a, "tv": self.a})
        before = self._state()
        snapshot = self._snapshot()
        payload = self._payload(snapshot)
        route = self._route(payload)
        self.assertEqual(route["default_destination_id"], "")
        self.assertEqual(route["overrides"], {"movie": self.a, "tv": self.a})
        self.assertIsNone(route["assignment_revisions"]["anime"])
        self.assertEqual(self._put(payload).status_code, 200)
        self.assertEqual(self._state(), before)
        self.assertEqual(self._snapshot()["routing_token"], snapshot["routing_token"])

    def test_complete_default_and_overrides_round_trip_is_lossless(self):
        self._configure(default=self.a, overrides={"tv": self.b})
        before = self._state()
        payload = self._payload(self._snapshot())
        self.assertEqual(self._route(payload)["default_destination_id"], self.a)
        self.assertEqual(self._route(payload)["overrides"], {"tv": self.b})
        self.assertEqual(self._put(payload).status_code, 200)
        self.assertEqual(self._state(), before)

    def test_stale_blank_source_cannot_delete_a_new_route(self):
        self._configure(default=self.a)
        stale = self._payload(self._snapshot())
        fresh = copy.deepcopy(stale)
        self._route(fresh, "tianyi")["default_destination_id"] = self.b
        self.assertEqual(self._put(fresh).status_code, 200)
        current = self._state()
        self._route(stale)["default_destination_id"] = self.b
        result = self._put(stale)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.get_json()["code"], "ASSIGNMENT_CONFLICT")
        self.assertEqual(self._state(), current)
        self.assertTrue(any(source == "tianyi" for source, _category in current))

    def test_stale_update_cannot_recreate_an_explicitly_deleted_route(self):
        self._configure(default=self.a)
        stale = self._payload(self._snapshot())
        delete = copy.deepcopy(stale)
        self._route(delete)["default_destination_id"] = ""
        self.assertEqual(self._put(delete).status_code, 200)
        self._route(stale)["default_destination_id"] = self.b
        self.assertEqual(self._put(stale).status_code, 409)
        self.assertEqual(self._state(), {})
        recreate = self._payload(self._snapshot())
        self._route(recreate)["default_destination_id"] = self.b
        self.assertEqual(self._put(recreate).status_code, 200)
        self.assertTrue(all(revision == 2 for _destination, revision in self._state().values()))

    def test_delete_recreate_aba_rejects_old_token_even_when_target_matches(self):
        self._configure(overrides={"movie": self.a})
        stale = self._payload(self._snapshot())
        self.assignments.delete("quark", "movie", expected_revision=1)
        recreated = self.assignments.upsert("quark", "movie", self.a, expected_revision=None)
        self.assertEqual(recreated.revision, 2)
        self.assertEqual(self._put(stale).status_code, 409)
        self.assertEqual(self._state(), {("quark", "movie"): (self.a, 2)})

    def test_empty_create_delete_aba_changes_token(self):
        empty = self._snapshot()
        created = self.assignments.upsert("quark", "movie", self.a, expected_revision=None, expected_token=empty["routing_token"])
        self.assignments.delete("quark", "movie", expected_revision=created.revision)
        self.assertEqual(self._state(), {})
        self.assertNotEqual(self._snapshot()["routing_token"], empty["routing_token"])
        self.assertEqual(self._put(self._payload(empty)).status_code, 409)
        self.assertEqual(self._put(self._payload(self._snapshot())).status_code, 200)

    def test_explicit_null_revision_means_absent_and_conflicts_atomically(self):
        self.assignments.upsert("quark", "movie", self.a, expected_revision=None)
        self.assignments.upsert("tianyi", "movie", self.a, expected_revision=None)
        token = self.assignments.snapshot()["routing_token"]
        before = self._state()
        with self.assertRaises(AssignmentConflictError):
            self.assignments.replace([
                {"search_source_id": "quark", "category_key": "movie", "destination_id": self.b, "expected_revision": 1},
                {"search_source_id": "tianyi", "category_key": "movie", "destination_id": self.b, "expected_revision": None},
            ], expected_token=token)
        self.assertEqual(self._state(), before)
        with self.assertRaises(AssignmentConflictError):
            self.assignments.upsert("quark", "movie", self.b, expected_revision=None)
        with self.assertRaises(AssignmentConflictError):
            self.assignments.delete("quark", "movie", expected_revision=None)
        self.assertFalse(self.assignments.delete("quark", "tv", expected_revision=None))

    def test_snapshot_gets_do_not_backfill_or_modify_routing_data(self):
        with self.fixture.db.connect() as conn:
            before = [tuple(row) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        snapshot = self._snapshot()
        for path in ("/api/admin/source-assignments", "/api/admin/routing-matrix"):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["routing_token"], snapshot["routing_token"])
        with self.fixture.db.connect() as conn:
            after = [tuple(row) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_destination_assignments").fetchone()[0], 0)
        self.assertEqual(after, before)

    def test_rows_and_token_are_from_one_read_snapshot(self):
        self._configure(overrides={"movie": self.a})
        expected = self.assignments.snapshot()
        other_db = RepositorySet.from_database(Database(self.fixture.db_path))
        other = SourceAssignmentService(other_db, self.fixture.targets)
        original = self.assignments._snapshot_token

        def write_after_rows_read(connection, rows):
            other.upsert("quark", "movie", self.b, expected_revision=1)
            return original(connection, rows)

        with patch.object(self.assignments, "_snapshot_token", side_effect=write_after_rows_read):
            old = self.assignments.snapshot()
        self.assertEqual(old["routing_token"], expected["routing_token"])
        self.assertEqual(old["assignments"][0].destination_id, self.a)
        self.assertEqual(self.assignments.get_assignment("quark", "movie").destination_id, self.b)
        with self.assertRaises(AssignmentConflictError):
            self.assignments.replace([], expected_token=old["routing_token"])

    def test_two_concurrent_replacements_have_one_winner(self):
        token = self.assignments.snapshot()["routing_token"]
        barrier = threading.Barrier(2)

        def write(destination):
            service = SourceAssignmentService(RepositorySet.from_database(Database(self.fixture.db_path)), self.fixture.targets)
            barrier.wait(timeout=3)
            try:
                service.replace([{"search_source_id": "quark", "category_key": "movie", "destination_id": destination,
                                  "expected_revision": None}], expected_token=token, require_snapshot=True)
                return "saved"
            except AssignmentConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, (self.a, self.b)))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(len(self._state()), 1)

    def test_both_http_write_entrypoints_require_a_snapshot(self):
        self._configure(default=self.a)
        before = self._state()
        for path, payload in (("/api/admin/storage-setup/routes", {"routes": []}),
                              ("/api/admin/source-assignments", {"assignments": []})):
            with self.subTest(path=path):
                response = self.client.put(path, json=payload)
                self.assertEqual(response.status_code, 428)
                self.assertEqual(response.get_json()["code"], "ASSIGNMENT_SNAPSHOT_REQUIRED")
                response = self.client.put(path, json={**payload, "routing_token": {"invalid": True}})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self._state(), before)

    def test_low_level_http_supports_safe_create_update_and_delete(self):
        path = "/api/admin/source-assignments"
        initial = self.client.get(path).get_json()
        item = {"search_source_id": "quark", "category_key": "movie", "destination_id": self.a, "expected_revision": None}
        created = self.client.put(path, json={"assignments": [item], "routing_token": initial["routing_token"]})
        self.assertEqual(created.status_code, 200)
        current = self.client.get(path).get_json()
        item.update(destination_id=self.b, expected_revision=1)
        updated = self.client.put(path, json={"assignments": [item], "routing_token": current["routing_token"]})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self._state(), {("quark", "movie"): (self.b, 2)})
        current = self.client.get(path).get_json()
        deleted = self.client.put(path, json={"assignments": [], "routing_token": current["routing_token"]})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self._state(), {})


class StorageRouteUiTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "Node unavailable")
    def test_browser_sends_full_token_and_preserves_form_on_conflict(self):
        script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
(async () => {
  const results = {};
  for (const scenario of ['success', 'conflict', 'missing']) {
    const handlers = {}, calls = [], notice = {}, data = {
      success: true, routing_token: 'routes-v1:' + 'a'.repeat(64),
      providers: [], accounts: [], connections: [], locations: [],
      progress: { completed: 0, total: 4, steps: [] }, categories: [{key:'movie',label:'Movie'}, {key:'tv',label:'TV'}],
      routes: [{search_source_id:'quark',default_destination_id:null,overrides:{movie:'dest-a'},assignment_revisions:{movie:1,tv:null}}]
    };
    if (scenario === 'missing') delete data.routing_token;
    let renders = 0;
    const host = { addEventListener: (kind, handler) => {handlers[kind] = handler;}, querySelectorAll: () => [], setAttribute: () => {} };
    Object.defineProperty(host, 'innerHTML', {set: () => {renders++;}, get: () => ''});
    const sandbox = {console, setTimeout, clearTimeout, HTMLButtonElement: class {},
      document: {getElementById: id => id === 'v1StorageWorkspace' ? host : id === 'storageSetupNotice' ? notice : null},
      FormData: class {constructor(form) {this.form=form;} entries() {return Object.entries(this.form.values);}},
      window: {sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}, FnosUI: {
        escapeHtml: value => String(value ?? ''),
        requestAdminJson: async (path, options) => {
          calls.push({path, ...(options || {})});
          if (path === '/api/admin/storage-setup') return structuredClone(data);
          if (scenario === 'success') return {success:true};
          return {success:false,code:scenario === 'conflict' ? 'ASSIGNMENT_CONFLICT' : 'ASSIGNMENT_SNAPSHOT_REQUIRED',message:'stale'};
        }
      }}
    };
    vm.runInNewContext(source, sandbox);
    await new Promise(resolve => setImmediate(resolve));
    const beforeRenders = renders;
    const form = {id:'storageRoutesForm',querySelector:()=>null,values:{route_quark:'',override_quark_movie:'dest-edited',override_quark_tv:''}};
    await handlers.submit({preventDefault(){},target:form});
    const puts = calls.filter(call => call.path.endsWith('/routes'));
    results[scenario] = {body:JSON.parse(puts[0].body),putCount:puts.length,gets:calls.filter(call=>call.path==='/api/admin/storage-setup').length,
      beforeRenders,renders,form:form.values,notice:notice.textContent};
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "static" / "admin-storage-v1.js")],
                                capture_output=True, text=True, encoding="utf-8", check=True, timeout=10)
        scenarios = json.loads(result.stdout)
        for name, scenario in scenarios.items():
            with self.subTest(scenario=name):
                self.assertEqual(scenario["putCount"], 1)
                self.assertEqual(scenario["body"]["routes"][0]["overrides"], {"movie": "dest-edited"})
                self.assertIsNone(scenario["body"]["routes"][0]["assignment_revisions"]["tv"])
                if name != "missing":
                    self.assertEqual(scenario["body"]["routing_token"], "routes-v1:" + "a" * 64)
                if name != "success":
                    self.assertEqual(scenario["gets"], 1)
                    self.assertEqual(scenario["renders"], scenario["beforeRenders"])
                    self.assertEqual(scenario["form"]["override_quark_movie"], "dest-edited")
                    self.assertIn("当前选择已保留", scenario["notice"])


if __name__ == "__main__":
    unittest.main()
