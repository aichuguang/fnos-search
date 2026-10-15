from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for UI regression tests")
class AdminJobActionFeedbackTests(unittest.TestCase):
    def run_ui(self, body: str) -> dict:
        script = r"""
globalThis.window = globalThis;
require(process.argv[1]);
require(process.argv[2]);
globalThis.renderListPagers = () => {};
async function runAction(action, scenario, { retry = false, entry = '' } = {}) {
    const calls = [], messages = [], elements = new Map(), handlers = {};
    let closed = 0, opened = 0, renders = 0;
    const row = {id:7,status:'failed',job_id:null,title:'Example',request_token:'same-token'};
    const button = {dataset:{requestRetry:'7',drawerRetry:'7'},addEventListener:(_kind,handler)=>{handlers.retry=handler;}};
    const getElement = id => {
        if (!elements.has(id)) {
            const element = {value:'',textContent:'',querySelectorAll:selector=>
                id==='adminRequestsBody' && selector==='[data-request-retry]' ? [button] : [],
                querySelector:selector=>id==='adminDrawerBody' && selector==='[data-drawer-retry]' ? button : null};
            let html = 'original-view';
            Object.defineProperty(element,'innerHTML',{get:()=>html,set:value=>{html=value;renders++;}});
            elements.set(id,element);
        }
        return elements.get(id);
    };
    const state = {requests:[row],jobs:[{id:7,status:'failed'}],selectedJobIds:[7],
        pagination:{requests:{page:1},jobs:{page:1}}};
    globalThis.fetch = async (path,options={}) => {
        const method=options.method||'GET';
        calls.push({path,method,body:options.body});
        if(method!=='GET' && scenario==='network') throw new Error('connection interrupted');
        const failed=method!=='GET' && ['http','business'].includes(scenario);
        const value=failed ? {success:false,message:'backend refusal'}
            : method==='GET' && path==='/api/admin/requests/7' ? {success:true,request:row,events:[]}
            : {success:true,message:'accepted',items:[],summary:{}};
        return {ok:!(failed&&scenario==='http'),status:failed&&scenario==='http'?409:200,json:async()=>value};
    };
    const ui=FnosAdminJobs.create({state,getElement,api:FnosUI.requestJson,
        toast:(message,type)=>messages.push({message,type}),escapeHtml:FnosUI.escapeHtml,
        icon:()=>'',statusPill:value=>String(value),statusText:value=>String(value),formatDate:()=>'-',
        normalizePagination:(_value,fallback)=>fallback,renderPager:()=>{},
        openDrawer:()=>{opened++;},closeDrawer:()=>{closed++;},
        promptDialog:async()=>scenario==='cancel'?null:'reason',confirmDialog:async()=>scenario!=='cancel',
        renderTimelineRawData:()=>'',loadRclone:async()=>{},
    });
    let rejected='';
    try {
        if(entry==='list') {
            ui.renderRequests();
            await handlers.retry();
        } else if(entry==='drawer') {
            await ui.showRequestDetail(7);
            await handlers.retry({currentTarget:button});
        } else if(action==='approveRequest') await ui[action](7,{retry});
        else await ui[action](7);
    } catch(error) { rejected=error.message; }
    return {calls,messages,closed,opened,renders,rejected,selected:state.selectedJobIds};
}
(async()=>{
""" + body + r"""
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        completed = subprocess.run([
            "node", "-e", script, str(ROOT / "static" / "ui-core.js"), str(ROOT / "static" / "admin-jobs.js"),
        ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=10)
        return json.loads(completed.stdout)

    def test_mutation_failures_surface_reason_without_refresh_close_or_retry(self) -> None:
        results = self.run_ui("""
const results={};
for(const scenario of ['http','business','network']) {
    for(const action of ['approveRequest','rejectRequest','cancelRequest','retryJob','cancelJob','deleteJob']) {
        results[scenario+':'+action]=await runAction(action,scenario);
    }
}
process.stdout.write(JSON.stringify(results));
""")
        for name, result in results.items():
            with self.subTest(name=name):
                self.assertEqual(result["rejected"], "")
                self.assertEqual(len(result["calls"]), 1)
                self.assertEqual((result["closed"], result["opened"], result["renders"]), (0, 0, 0))
                self.assertEqual(result["selected"], [7])
                self.assertEqual(result["messages"], [{
                    "message": "connection interrupted" if name.startswith("network:") else "backend refusal", "type": "error",
                }])

    def test_successful_actions_still_submit_once_and_refresh(self) -> None:
        results = self.run_ui("""
const results={};
for(const action of ['approveRequest','rejectRequest','cancelRequest','retryJob','cancelJob','deleteJob']) {
    results[action]=await runAction(action,'success');
}
process.stdout.write(JSON.stringify(results));
""")
        for action, result in results.items():
            with self.subTest(action=action):
                self.assertEqual(result["rejected"], "")
                self.assertEqual(len([call for call in result["calls"] if call["method"] != "GET"]), 1)
                self.assertGreater(len(result["calls"]), 1)
                self.assertTrue(any(message["type"] == "success" for message in result["messages"]))
                self.assertFalse(any(message["type"] == "error" for message in result["messages"]))

    def test_cancelled_confirmation_does_not_submit(self) -> None:
        results = self.run_ui("""
const results={};
for(const action of ['rejectRequest','cancelRequest','cancelJob','deleteJob']) results[action]=await runAction(action,'cancel');
process.stdout.write(JSON.stringify(results));
""")
        for result in results.values():
            self.assertEqual(result["rejected"], "")
            self.assertEqual(result["calls"], [])
            self.assertEqual(result["messages"], [])
            self.assertEqual(result["closed"], 0)

    def test_retry_entry_only_appears_for_failed_unbound_request(self) -> None:
        results = self.run_ui("""
const ui=FnosAdminJobs.create({state:{},escapeHtml:FnosUI.escapeHtml});
const results={};
for(const [status,job_id] of [['failed',null],['failed',12],['cancelled',null],['done',null],['pending_review',null]]) {
    const item={id:7,status,job_id};
    results[status+':'+job_id]={list:ui.requestActionButtons(item),drawer:ui.requestDrawerActions(item)};
}
process.stdout.write(JSON.stringify(results));
""")
        for name, result in results.items():
            for html in result.values():
                with self.subTest(name=name):
                    self.assertEqual("重试提交" in html, name == "failed:null")

    def test_both_retry_buttons_send_explicit_retry_flag_on_original_request(self) -> None:
        results = self.run_ui("""
process.stdout.write(JSON.stringify({list:await runAction('approveRequest','success',{entry:'list'}),
    drawer:await runAction('approveRequest','success',{entry:'drawer'})}));
""")
        for entry, result in results.items():
            with self.subTest(entry=entry):
                self.assertEqual(result["rejected"], "")
                writes = [call for call in result["calls"] if call["method"] == "POST"]
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0]["path"], "/api/admin/requests/7/approve")
                self.assertEqual(json.loads(writes[0]["body"]), {"retry": True})


if __name__ == "__main__":
    unittest.main()
