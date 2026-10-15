from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.storage.setup_blueprint import StorageSetupBlueprintContext, create_storage_setup_blueprint
from tests import test_storage_setup_v1 as storage_fixtures


ROOT = Path(__file__).resolve().parents[1]


class StorageSetupMutationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = storage_fixtures.StorageSetupV1Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        app = Flask(__name__)
        app.register_blueprint(create_storage_setup_blueprint(StorageSetupBlueprintContext(
            admin_required=lambda handler: handler, service=self.fixture.service,
        )))
        self.client = app.test_client()

    def activation(self, account_id: str, **payload):
        return self.client.post("/api/admin/storage-setup/accounts", json={
            "account_id": account_id, "activate": True, **payload,
        })

    def test_patch_missing_or_deleted_location_never_creates_or_probes(self) -> None:
        deleted_id = self.fixture.service.save_location({"kind": "local", "name": "Removed"})["destination_id"]
        self.fixture.service.delete_location(deleted_id)
        with patch.object(self.fixture.mounts, "probe_mount") as probe, patch.object(self.fixture.targets, "create_target") as create:
            for destination_id, kind in (("never-created", "local"), (deleted_id, "local"), (deleted_id, "provider")):
                with self.subTest(destination_id=destination_id, kind=kind):
                    response = self.client.patch(f"/api/admin/storage-setup/locations/{destination_id}", json={
                        "kind": kind, "name": "Stale editor", "expected_revision": 1,
                    })
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.get_json()["code"], "TARGET_NOT_FOUND")
            create.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(self.fixture.targets.list_targets(), [])
        self.assertEqual(self.fixture.mounts.list_mounts(), [])

    def test_stale_account_selection_is_rejected_without_renaming_or_switching(self) -> None:
        first = self.fixture._account("quark", "First")
        stale_revision = self.fixture.accounts.get_selection("quark").selection_revision
        second = self.fixture._account("quark", "Second")
        self.fixture.accounts.update_account_name(first, "New first name")
        before = self.fixture.accounts.get_selection("quark")
        response = self.activation(first, expected_selection_revision=stale_revision)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "SELECTION_CHANGED")
        self.assertEqual(self.fixture.accounts.get_selection("quark"), before)
        self.assertEqual(before.account_id, second)
        self.assertEqual(self.fixture.accounts.get_account(first).name, "New first name")

    def test_snapshot_exposes_selection_version_and_fresh_activation_succeeds(self) -> None:
        first = self.fixture._account("quark", "First")
        second = self.fixture._account("quark", "Second")
        snapshot = self.client.get("/api/admin/storage-setup").get_json()
        selected = snapshot["active_selections"]["quark:default"]
        self.assertEqual(selected["account_id"], second)
        response = self.activation(first, expected_selection_revision=selected["selection_revision"])
        self.assertEqual(response.status_code, 200)
        selection = self.fixture.accounts.get_selection("quark")
        self.assertEqual(selection.account_id, first)
        self.assertEqual(selection.selection_revision, selected["selection_revision"] + 1)
        self.assertEqual(self.fixture.accounts.get_account(first).name, "First")

    def test_activation_requires_an_explicit_valid_selection_snapshot(self) -> None:
        account_id = self.fixture._account("quark", "First")
        before = self.fixture.accounts.get_selection("quark")
        response = self.activation(account_id)
        self.assertEqual(response.status_code, 428)
        self.assertEqual(response.get_json()["code"], "ACCOUNT_SELECTION_SNAPSHOT_REQUIRED")
        for value in (True, 0, -1, 1.5, "1", [], {}):
            with self.subTest(value=value):
                response = self.activation(account_id, expected_selection_revision=value)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.fixture.accounts.get_selection("quark"), before)

    def test_explicit_null_can_create_first_selection_but_cannot_replace_one(self) -> None:
        account = self.fixture.accounts.create_account(
            provider_kind="quark", name="Unselected", config={}, credentials={"cookie": "test-only"},
        )
        self.fixture.accounts.test_account(account.account_id)
        self.assertIsNone(self.fixture.accounts.get_selection("quark"))
        self.assertEqual(self.activation(account.account_id, expected_selection_revision=None).status_code, 200)
        self.assertEqual(self.activation(account.account_id, expected_selection_revision=None).status_code, 409)

    def test_rejected_account_fields_do_not_change_name_or_selection(self) -> None:
        account_id = self.fixture._account("quark", "Original")
        before = self.fixture.accounts.get_selection("quark")
        for forbidden in ({"credentials": {}}, {"config": {}}, {"activate": True}):
            with self.subTest(forbidden=forbidden):
                response = self.client.post("/api/admin/storage-setup/accounts", json={
                    "account_id": account_id, "name": "Rejected rename", "activate": False,
                    "expected_selection_revision": before.selection_revision, **forbidden,
                })
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.fixture.accounts.get_account(account_id).name, "Original")
                self.assertEqual(self.fixture.accounts.get_selection("quark"), before)

    def test_name_only_update_does_not_activate_another_account(self) -> None:
        first = self.fixture._account("quark", "First")
        self.fixture._account("quark", "Second")
        before = self.fixture.accounts.get_selection("quark")
        response = self.client.post("/api/admin/storage-setup/accounts", json={
            "account_id": first, "name": "Renamed first", "activate": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fixture.accounts.get_account(first).name, "Renamed first")
        self.assertEqual(self.fixture.accounts.get_selection("quark"), before)

    def test_disabled_account_is_still_rejected_by_activation_service(self) -> None:
        first = self.fixture._account("quark", "First")
        self.fixture._account("quark", "Second")
        self.fixture.accounts.disable_account(first)
        before = self.fixture.accounts.get_selection("quark")
        response = self.activation(first, expected_selection_revision=before.selection_revision)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "ACCOUNT_DISABLED")
        self.assertEqual(self.fixture.accounts.get_selection("quark"), before)


class StorageSetupAccountContractTests(unittest.TestCase):
    def test_account_activation_documents_snapshot_and_separate_rename(self) -> None:
        spec = get_openapi_spec()
        operation = spec["paths"]["/api/admin/storage-setup/accounts"]["post"]
        self.assertEqual(operation["security"], [{"cookieAuth": []}])
        self.assertTrue({"409", "422", "428"}.issubset(operation["responses"]))
        self.assertIn("ACCOUNT_SELECTION_SNAPSHOT_REQUIRED", operation["responses"]["428"]["description"])
        schemas = spec["components"]["schemas"]
        activation = schemas["StorageSetupAccountActivationRequest"]
        self.assertIn("expected_selection_revision", activation["required"])
        self.assertEqual(activation["properties"]["expected_selection_revision"]["minimum"], 1)
        self.assertTrue(activation["properties"]["expected_selection_revision"]["nullable"])
        self.assertFalse(activation["additionalProperties"])
        self.assertNotIn("name", activation["properties"])
        self.assertEqual(schemas["StorageSetupAccountRenameRequest"]["properties"]["activate"]["enum"], [False])
        snapshot = spec["paths"]["/api/admin/storage-setup"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertIn("active_selections", snapshot["required"])
        self.assertIn("routing_token", snapshot["required"])


@unittest.skipUnless(shutil.which("node"), "Node is required for UI regression tests")
class StorageSetupMutationUiTests(unittest.TestCase):
    def run_ui(self, body: str) -> dict:
        script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const handlers = {}, calls = [], notice = {};
const data = { success: true, providers: [], connections: [], categories: [], routes: [],
    accounts: [{account_id:'account-a',provider_kind:'quark',name:'Stale name',enabled:true,last_test_status:'ok'}],
    active_selections: {'quark:default':{account_id:'account-b',selection_revision:7}},
    locations: [{destination_id:'local-a',kind:'local',provider_kind:'local',name:'Original',current_revision:1}],
    progress: {completed:0,total:4,steps:[]},
};
let renders = 0, failure = '', selectionExists = true;
const host = {addEventListener:(kind,handler)=>{handlers[kind]=handler;},querySelectorAll:()=>[],setAttribute:()=>{}};
Object.defineProperty(host,'innerHTML',{set:()=>{renders++;},get:()=>''});
const sandbox = {console,setTimeout,clearTimeout,HTMLButtonElement:class{},
    document:{getElementById:id=>id==='v1StorageWorkspace'?host:id==='storageSetupNotice'?notice:null},
    FormData:class{constructor(form){this.form=form;}entries(){return Object.entries(this.form.values);}},
    window:{sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},FnosUI:{
        escapeHtml:value=>String(value??''),
        requestAdminJson:async(path,options)=>{
            calls.push({path,...(options||{})});
            if(path==='/api/admin/storage-setup') {
                const snapshot=structuredClone(data);
                if(!selectionExists) snapshot.active_selections={};
                return snapshot;
            }
            return failure?{success:false,code:failure,message:'conflict'}:{success:true};
        },
    }},
};
vm.runInNewContext(source,sandbox);
const click=async(dataset)=>{
    const button={dataset};
    await handlers.click({target:{closest:selector=>selector==='button'?button:null}});
};
(async()=>{
    await new Promise(resolve=>setImmediate(resolve));
""" + body + r"""
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        completed = subprocess.run(["node", "-e", script, str(ROOT / "static" / "admin-storage-v1.js")],
                                   check=True, capture_output=True, text=True, encoding="utf-8", timeout=10)
        return json.loads(completed.stdout)

    def test_activation_sends_snapshot_without_name_and_never_retries_stale_selection(self) -> None:
        result = self.run_ui("""
failure='SELECTION_CHANGED';
await click({accountAction:'activate',id:'account-a'});
process.stdout.write(JSON.stringify({calls,notice:notice.textContent}));
""")
        posts = [call for call in result["calls"] if call.get("method") == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]["body"]), {
            "account_id": "account-a", "activate": True, "expected_selection_revision": 7,
        })
        self.assertEqual(len(result["calls"]), 2)

    def test_first_activation_sends_explicit_null_selection(self) -> None:
        result = self.run_ui("""
selectionExists=false;
await click({setupAction:'refresh'});
await click({accountAction:'activate',id:'account-a'});
process.stdout.write(JSON.stringify({calls}));
""")
        posts = [call for call in result["calls"] if call.get("method") == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIsNone(json.loads(posts[0]["body"])["expected_selection_revision"])

    def test_location_conflict_preserves_input_until_explicit_refresh_and_reopen(self) -> None:
        result = self.run_ui("""
await click({locationAction:'edit',id:'local-a'});
const form={id:'storageLocationForm',querySelector:()=>null,values:{name:'Unsaved edit',kind:'local'}};
const beforeRenders=renders;
failure='TARGET_CONFLICT';
data.locations[0].current_revision=2;
await handlers.submit({preventDefault(){},target:form});
const rejected={calls:[...calls],renders,beforeRenders,form:{...form.values},notice:notice.textContent};
await click({setupAction:'cancel-editor'});
await click({setupAction:'refresh'});
await click({locationAction:'edit',id:'local-a'});
failure='';
await handlers.submit({preventDefault(){},target:form});
process.stdout.write(JSON.stringify({calls,rejected}));
""")
        self.assertEqual(result["rejected"]["renders"], result["rejected"]["beforeRenders"])
        self.assertEqual(len([call for call in result["rejected"]["calls"] if call["path"] == "/api/admin/storage-setup"]), 1)
        self.assertEqual(result["rejected"]["form"]["name"], "Unsaved edit")
        self.assertIn("当前输入已保留", result["rejected"]["notice"])
        self.assertIn("重新打开", result["rejected"]["notice"])
        writes = [json.loads(call["body"]) for call in result["calls"] if call.get("method") == "PATCH"]
        self.assertEqual([body["expected_revision"] for body in writes], [1, 2])


if __name__ == "__main__":
    unittest.main()
