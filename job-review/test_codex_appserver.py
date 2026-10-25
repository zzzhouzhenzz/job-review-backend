"""Exercise the supported JSONL protocol with a fictional local executable."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from codex_appserver import (APPROVAL_POLICY, AppServerError, AppServerResult, _stop,
                             inspect_inactive_turn, run_appserver)
from codex_transport import CodexTransport, CodexTransportError
from readiness import BrowserRecoverable, BrowserWorkUncertain, WorkerCapacityUnavailable


THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
FIXTURE = r'''
import json, pathlib, subprocess, sys, time
root=pathlib.Path.cwd()
mode=(root/"scenario").read_text()
thread="11111111-1111-4111-8111-111111111111"
turn="22222222-2222-4222-8222-222222222222"
other="33333333-3333-4333-8333-333333333333"
def emit(value): print(json.dumps(value),flush=True)
def receive():
    value=json.loads(sys.stdin.readline())
    with (root/"received.jsonl").open("a") as out: out.write(json.dumps(value)+"\n")
    return value
def reply(request,result): emit({"id":request["id"],"result":result})
def notify(method,params): emit({"method":method,"params":params})
def tool(status="completed",error=None):
    return {"id":"browser-1","type":"mcpToolCall","server":"cua_repl","tool":"js",
            "arguments":{},"status":status,"error":error,"result":{"content":[]}}
def item(value,completed=True,tid=turn):
    notify("item/completed" if completed else "item/started",
           {"threadId":thread,"turnId":tid,"item":value})
init=receive()
if mode=="slow-init": time.sleep(10)
reply(init,{"userAgent":"fictional"})
receive()  # initialized
if mode.startswith("inspect-"):
    snapshots=0
    for line in sys.stdin:
        request=json.loads(line)
        with (root/"received.jsonl").open("a") as out: out.write(json.dumps(request)+"\n")
        if mode=="inspect-error":
            emit({"id":request["id"],"error":{"code":-32602,"message":"private failure"}})
            continue
        if request["method"]=="thread/read":
            reply(request,{"thread":{"id":other if mode=="inspect-wrong-thread" else thread,
                 "status":{"type":"active" if mode=="inspect-active-thread" else "notLoaded"}}})
        elif request["method"]=="thread/turns/list":
            snapshots+=1
            items=[] if mode=="inspect-empty" else [
                {"id":"u","type":"userMessage","content":[{"text":"private request"}]},
                {"id":"a","type":"agentMessage","text":"private answer"},
                {"id":"r","type":"reasoning","text":"private reasoning"},
                {"id":"p","type":"plan"}, {"id":"c","type":"contextCompaction"}]
            if mode=="inspect-tool": items.append(tool())
            if mode=="inspect-subagent": items.append({"id":"child","type":"subAgentActivity","agentThreadId":other})
            if mode=="inspect-unknown-item": items.append({"id":"unknown","type":"futureNativeAction"})
            if mode=="inspect-many-items": items=[{"id":str(i),"type":"reasoning"} for i in range(257)]
            if mode=="inspect-duplicate-items": items.append(items[0])
            status="inProgress" if mode=="inspect-active-turn" else "interrupted"
            if mode=="inspect-completed": status="completed"
            if mode=="inspect-failed": status="failed"
            value={"id":other if mode=="inspect-wrong-turn" or mode=="inspect-changed" and snapshots==2 else turn,
                   "status":status,"items":items,"itemsView":"summary" if mode=="inspect-partial" else "full",
                   "startedAt":100,"completedAt":200}
            if mode=="inspect-missing-end": value.pop("completedAt")
            reply(request,{"data":[value],"nextCursor":"older-turns-exist"})
            if mode=="inspect-truncated" and snapshots==2: print('{"method":',end="",flush=True)
        else:
            raise RuntimeError("inspection sent a mutating or unknown method")
    sys.exit(0)
start=receive()
list_pages=0
while start["method"]=="thread/list":
    list_pages+=1
    queried=start["params"]["parentThreadId"]
    children=[]
    cursor=None
    if mode.startswith("preload-") and queried==thread:
        children=[{"id":other,"parentThreadId":thread}]
    if mode=="preload-grandchild" and queried==other:
        children=[{"id":"44444444-4444-4444-8444-444444444444","parentThreadId":other}]
    if mode=="preload-wrong-parent": children=[{"id":other,"parentThreadId":other}]
    if mode=="preload-bad-id": children=[{"id":"private invalid","parentThreadId":thread}]
    if mode=="preload-duplicate": children=children*2
    if mode=="preload-cycle" and queried==other: children=[{"id":thread,"parentThreadId":other}]
    if mode=="preload-overflow":
        children=[{"id":f"{i:08x}-1111-4111-8111-111111111111","parentThreadId":queried}
                  for i in (range(64) if queried==thread else [64])]
    if mode in ("preload-pagination", "preload-repeated-cursor", "preload-page-limit") and queried==thread:
        children=[{"id":f"{list_pages:08x}-2222-4222-8222-222222222222","parentThreadId":thread}]
        if mode!="preload-pagination" or list_pages==1:
            cursor="same" if mode=="preload-repeated-cursor" else "page-"+str(list_pages)
    if mode=="preload-bad-cursor": cursor=42
    for child in children: child["status"]={"type":"notLoaded"}
    if mode=="preload-active-child": children[0]["status"]={"type":"active","activeFlags":[]}
    if mode=="preload-unknown-status": children[0]["status"]={"type":"private unknown status"}
    if mode=="preload-missing-status": children[0].pop("status")
    if mode=="preload-system-error": children[0]["status"]={"type":"systemError"}
    page={"data":children,"nextCursor":cursor}
    if mode=="preload-missing-cursor": page.pop("nextCursor")
    reply(start,page)
    start=receive()
thread=start["params"].get("threadId",thread)
if mode=="resume-active-writer":
    emit({"id":start["id"],"error":{"code":-32600,
        "message":f"thread {thread} already has an active writer"}})
    sys.stdin.read()
    sys.exit(0)
if mode=="wrong-thread": thread=other
policy=start["params"]["approvalPolicy"]
if mode in ("unsafe-policy", "preload-early-child-unsafe-policy"): policy="never"
notify("thread/started",{"thread":{"id":thread}})
if mode in ("preload-child-events", "preload-grandchild", "preload-child-approval", "preload-capacity",
            "preload-unknown-action", "preload-early-child-unsafe-policy"):
    notify("thread/tokenUsage/updated",{"threadId":other,"turnId":other,"tokenUsage":{}})
if mode=="preload-early-child-unsafe-policy":
    notify("turn/started",{"threadId":other,"turn":{"id":other,"items":[],"status":"inProgress"}})
if mode.startswith("historical-usage"):
    notify("thread/tokenUsage/updated",{"threadId":other if mode=="historical-usage-wrong-thread" else thread,
        "turnId":other,"tokenUsage":{}})
if mode.startswith("runtime-status") or mode=="child-events-status-before-link":
    for identity in (other,thread):
        for status in ({"type":"notLoaded"},{"type":"idle"},{"type":"systemError"},
                       {"type":"active","activeFlags":["waitingOnApproval","waitingOnUserInput"]}):
            notify("thread/status/changed",{"threadId":identity,"status":status})
    malformed={
        "runtime-status-bad-id":{"threadId":"private invalid identity","status":{"type":"idle"}},
        "runtime-status-missing":{"threadId":other},
        "runtime-status-unknown":{"threadId":other,"status":{"type":"private new status"}},
        "runtime-status-bad-type":{"threadId":other,"status":{"type":{}}},
        "runtime-status-missing-flags":{"threadId":other,"status":{"type":"active"}},
        "runtime-status-bad-flags":{"threadId":other,"status":{"type":"active","activeFlags":[{}]}}}
    if mode in malformed: notify("thread/status/changed",malformed[mode])
reply(start,{"thread":{"id":thread},"approvalPolicy":policy,
             "sandbox":{"type":"readOnly","networkAccess":False}})
request=receive()
if request['method']=='thread/name/set':
    notify('thread/name/updated',{'threadId':thread,'threadName':request['params']['name']})
    reply(request,{})
    request=receive()
if mode=="reject-turn":
    emit({"id":request["id"],"error":{"code":-32602,"message":"private schema validation error", "data":{"field":"outputSchema"}}})
    sys.stdin.read()
    sys.exit(0)
if mode=="lost-turn-response": sys.exit(0)
notify("turn/started",{"threadId":thread,"turn":{"id":turn,"items":[],"status":"inProgress"}})
if mode in ("reject-after-start", "reject-after-tool"):
    if mode=="reject-after-tool": item(tool("inProgress"),False)
    emit({"id":request["id"],"error":{"code":-32602,"message":"private late rejection"}})
    sys.stdin.read()
    sys.exit(0)
reply(request,{"turn":{"id":turn,"items":[],"status":"inProgress"}})
(root/"turn-started").write_text("ready")
if mode in ("preload-child-events", "preload-grandchild", "preload-child-approval"):
    source="44444444-4444-4444-8444-444444444444" if mode=="preload-grandchild" else other
    child_turn="55555555-5555-4555-8555-555555555555"
    notify("turn/started",{"threadId":source,"turn":{"id":child_turn,"items":[],"status":"inProgress"}})
    notify("item/completed",{"threadId":source,"turnId":child_turn,"item":tool()})
    if mode=="preload-child-approval":
        emit({"id":77,"method":"mcpServer/elicitation/request","params":{
            "threadId":source,"turnId":child_turn,"serverName":"cua_repl","mode":"openai/form",
            "message":"private upload","requestedSchema":{"type":"object"}}})
        receive()
    notify("turn/completed",{"threadId":source,"turn":{"id":child_turn,"items":[],"status":"completed"}})
if mode=="preload-unknown-action":
    notify("item/completed",{"threadId":"44444444-4444-4444-8444-444444444444","turnId":turn,"item":tool()})
if mode=="runtime-status-unlinked-tool":
    notify("item/completed",{"threadId":other,"turnId":turn,"item":tool()})
if mode=="runtime-status-unlinked-turn":
    notify("turn/completed",{"threadId":other,"turn":{"id":turn,"items":[],"status":"completed"}})
if mode=="runtime-status-unlinked-approval":
    emit({"id":77,"method":"mcpServer/elicitation/request","params":{
        "threadId":other,"turnId":turn,"serverName":"cua_repl","mode":"openai/form",
        "message":"private upload","requestedSchema":{"type":"object"}}})
    receive()
if mode.startswith("child-events"):
    child_turn="44444444-4444-4444-8444-444444444444"
    if mode!="child-events-parent-link":
        item({"id":"child-link","type":"subAgentActivity","kind":"interacted",
              "agentThreadId":other,"agentPath":"/root/fictional_evidence"})
    notify("thread/started",{"thread":{"id":other,"parentThreadId":thread}})
    notify("turn/started",{"threadId":other,"turn":{"id":child_turn,"items":[],"status":"inProgress"}})
    notify("item/completed",{"threadId":other,"turnId":child_turn,"item":tool()})
    notify("item/completed",{"threadId":other,"turnId":child_turn,"item":{
        "id":"child-result","type":"agentMessage","phase":"final_answer","text":'{"ok":false}'}})
    if mode in ("child-events-parent-callback", "child-events-parent-callback-no-browser",
                "child-events-invalid-parent-spawn"):
        interaction={"id":"child-to-parent","type":"subAgentActivity","agentThreadId":thread,
                     "agentPath":"/root","kind":"started" if mode=="child-events-invalid-parent-spawn" else "interacted"}
        notify("item/started",{"threadId":other,"turnId":child_turn,"item":interaction})
        notify("item/completed",{"threadId":other,"turnId":child_turn,"item":interaction})
    if mode=="child-events-approval":
        emit({"id":77,"method":"mcpServer/elicitation/request","params":{
            "threadId":other,"turnId":child_turn,"serverName":"cua_repl","mode":"openai/form",
            "message":"private child upload", "requestedSchema":{"type":"object"}}})
        receive()
    if mode not in ("child-events-late-completion","child-events-active","child-events-active-status-idle"):
        notify("turn/completed",{"threadId":other,"turn":{"id":child_turn,"items":[],"status":"completed"}})
    if mode=="child-events-active-status-idle":
        notify("thread/status/changed",{"threadId":other,"status":{"type":"idle"}})
if mode=="unlinked-child-events":
    notify("thread/started",{"thread":{"id":other,"parentThreadId":other}})
if mode=="invalid-parent-self-interaction":
    item({"id":"self-link","type":"subAgentActivity","agentThreadId":thread,"kind":"interacted"},False)
if mode=="unlinked-parent-callback":
    notify("item/started",{"threadId":other,"turnId":turn,"item":{
        "id":"unlinked-callback","type":"subAgentActivity","agentThreadId":thread,"kind":"interacted"}})
if mode=="duplicate-response": reply(request,{"turn":{"id":turn}})
if mode=="bad-json": print('{"private":NaN}',flush=True); time.sleep(10)
if mode=="large-line": print("X"*5000,flush=True); time.sleep(10)
if mode=="slow-turn": time.sleep(10)
if mode=="child":
    subprocess.Popen([sys.executable,"-c","import pathlib,time; time.sleep(1); pathlib.Path('child-marker').write_text('bad')"])
    item(tool("inProgress"),False)
    (root/"child-started").write_text("ready")
    time.sleep(10)
if mode.startswith("elicitation") or mode in ["verification","url","unknown-request"]:
    params={"threadId":thread,"turnId":turn,"serverName":"cua_repl","mode":"openai/form",
            "message":"Fictional resume upload approval", "requestedSchema":{"type":"object"}}
    method="mcpServer/elicitation/request"
    if mode=="elicitation-wrong-turn": params["turnId"]=other
    if mode=="elicitation-no-turn": del params["turnId"]
    if mode=="verification": params["mode"]="openai/userVerification"
    if mode=="url": params["mode"]="url"
    if mode=="elicitation-unsupported-mode": params["mode"]="private unsupported mode"
    if mode=="unknown-request": method="item/permissions/requestApproval"
    emit({"id":77,"method":method,"params":params})
    receive()
    if mode=="elicitation-duplicate":
        emit({"id":77,"method":method,"params":params})
        receive()
if mode.startswith("capacity") or mode=="preload-capacity":
    if mode=="capacity-after-tool": item(tool("inProgress"),False)
    if mode=="capacity-mixed":
        notify("error",{"threadId":thread,"turnId":turn,"error":{"message":"Authentication failed: private"},"willRetry":False})
    error={"message":"Selected model is at capacity. Try again later."}
    if mode=="capacity-conflicting-code": error["codexErrorInfo"]="unauthorized"
    notify("turn/completed",{"threadId":thread,"turn":{"id":turn,"items":[],"status":"failed","error":error}})
    if mode=="capacity-stderr": print("ERROR private credentials could not be loaded",file=sys.stderr,flush=True)
    if mode=="capacity-truncated": print('{"method":',end="",flush=True)
else:
    if mode not in ("no-browser", "historical-usage-no-browser", "child-events-no-parent-browser", "runtime-status-no-browser",
                    "child-events-parent-callback-no-browser"):
        if mode=="failed-browser": item(tool("failed",{"message":"private error"}))
        elif mode=="browser-result-error":
            value=tool(); value["result"]["isError"]=True; item(value)
        else: item(tool(),tid=other if mode=="wrong-turn" else turn)
    result='{"ok":true}'
    if mode=="bad-final": result='private invalid result'
    if mode=="duplicate-final-json": result='{"ok":true,"ok":false}'
    item({"id":"message-1","type":"agentMessage","text":"Progress only","phase":"commentary"})
    if mode not in ("child-events-no-parent-final", "runtime-status-no-final"):
        item({"id":"message-2","type":"agentMessage","text":result,"phase":"final_answer"})
    if mode=="failed-turn":
        notify("turn/completed",{"threadId":thread,"turn":{"id":turn,"items":[],"status":"failed","error":{"message":"private failure"}}})
    elif mode not in ("child-events-no-parent-completion", "runtime-status-no-completion"):
        error={"message":"private inconsistent failure"} if mode=="completed-with-error" else None
        notify("turn/completed",{"threadId":thread,"turn":{"id":turn,"items":[],"status":"completed","error":error}})
if mode=="child-events-late-completion":
    time.sleep(.1)
    notify("turn/completed",{"threadId":other,"turn":{"id":child_turn,"items":[],"status":"completed"}})
if mode=="shutdown-hang": time.sleep(10)
sys.stdin.read()
if mode=="exit-error": sys.exit(7)
'''


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "fictional-codex"
        self.binary.write_text("#!" + sys.executable + "\n" + FIXTURE)
        self.binary.chmod(0o700)
        self.closed = threading.Event()
        self.beats = 0
        self.approvals = []

    def heartbeat(self):
        self.beats += 1
        return True

    def approve(self, params):
        self.approvals.append(params)
        return {"action": "accept", "content": {"approved": True}}

    def run_fixture(self, mode="success", **overrides):
        (self.root / "scenario").write_text(mode)
        options = dict(binary=self.binary, cwd=self.root, prompt="fictional private prompt",
                       schema=SCHEMA, thread_id=None, timeout=4, heartbeat_interval=.05,
                       heartbeat=self.heartbeat, closed=self.closed, approve_request=self.approve)
        options.update(overrides)
        return run_appserver(**options)

    def received(self):
        return [json.loads(v) for v in (self.root / "received.jsonl").read_text().splitlines()]

    def assert_failure(self, mode, **overrides):
        with self.assertRaises(AppServerError) as caught:
            self.run_fixture(mode, **overrides)
        self.assertNotIn("private", str(caught.exception))
        return caught.exception

    def test_start_protocol_preserves_model_effort_and_reviewer(self):
        result = self.run_fixture()
        self.assertEqual(result.result, {"ok": True})
        self.assertEqual(result.thread_id, THREAD)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.turn_completed and result.browser_verified)
        self.assertEqual(result.tools, (("cua_repl", "js"),))
        self.assertGreater(result.event_bytes, 0)
        self.assertEqual(result.approval_events, ())
        values = self.received()
        self.assertEqual([v["method"] for v in values],
                         ["initialize", "initialized", "thread/start", "turn/start"])
        self.assertTrue(values[0]["params"]["capabilities"]["mcpServerOpenaiFormElicitation"])
        self.assertEqual(values[2]["params"]["approvalPolicy"], APPROVAL_POLICY)
        self.assertEqual(values[2]["params"]["sandbox"], "read-only")
        self.assertEqual(values[3]["params"]["outputSchema"], SCHEMA)
        for value in values:
            self.assertFalse({"model", "effort", "approvalsReviewer"} & set(value["params"]))
            if value['method'] == 'thread/start':
                self.assertEqual({'features.multi_agent': False, 'features.multi_agent_v2': False}, value['params']['config'])
                self.assertIn('Do not spawn subagents', value['params']['developerInstructions'])
        self.assertGreaterEqual(self.beats, 3)

    def test_resume_uses_exact_existing_thread(self):
        result = self.run_fixture(thread_id=THREAD)
        self.assertEqual(result.thread_id, THREAD)
        self.assertEqual(self.received()[2]["method"], "thread/list")
        self.assertEqual(self.received()[2]["params"]["parentThreadId"], THREAD)
        value = self.received()[3]
        self.assertEqual(value["method"], "thread/resume")
        self.assertEqual(value["params"]["threadId"], THREAD)
        self.assertTrue(value["params"]["excludeTurns"])
        self.assertEqual({'features.multi_agent': False, 'features.multi_agent_v2': False}, value['params']['config'])

    def test_shared_worker_name_is_set_before_turn_without_an_extra_thread(self):
        result = self.run_fixture(thread_id=THREAD, thread_name='Job Review worker 2')
        values = self.received()
        names = [v for v in values if v['method'] == 'thread/name/set']
        self.assertEqual([{'threadId': THREAD, 'name': 'Job Review worker 2'}], [v['params'] for v in names])
        self.assertNotIn('thread/start', [v['method'] for v in values])
        self.assertEqual(TURN, result.turn_id)

    def test_resume_preloads_bounded_native_parent_graph_before_helper_events(self):
        for mode in ("preload-child-events", "preload-grandchild", "preload-pagination"):
            with self.subTest(mode=mode):
                before = len(self.received()) if (self.root / "received.jsonl").exists() else 0
                result = self.run_fixture(mode, thread_id=THREAD)
                self.assertEqual(result.result, {"ok": True})
                self.assertEqual(result.tools, (("cua_repl", "js"),))
                values = self.received()[before:]
                resume = next(i for i, value in enumerate(values) if value.get("method") == "thread/resume")
                self.assertTrue(all(value["method"] == "thread/list" for value in values[2:resume]))
                self.assertEqual(values[2]["params"]["parentThreadId"], THREAD)
                for value in values[2:resume]:
                    self.assertEqual(value["params"]["limit"], 64)
                    self.assertIn("subAgentThreadSpawn", value["params"]["sourceKinds"])
                if mode == "preload-grandchild":
                    self.assertEqual(resume - 2, 3)
                if mode == "preload-pagination":
                    self.assertEqual(resume - 2, 4)
                    self.assertEqual(values[3]["params"]["cursor"], "page-1")
                self.assertTrue(values[resume]["params"]["excludeTurns"])

    def test_resume_rejects_partial_or_invalid_child_graph_before_starting_work(self):
        for suffix in ("wrong-parent", "bad-id", "duplicate", "cycle", "overflow",
                       "bad-cursor", "missing-cursor", "repeated-cursor", "page-limit"):
            with self.subTest(suffix=suffix):
                error = self.assert_failure("preload-" + suffix, thread_id=THREAD)
                self.assertFalse(error.uncertain)
                self.assertFalse(error.capacity)
                self.assertEqual(error.thread_id, THREAD)
                self.assertIsNone(error.turn_id)
                methods = [value["method"] for value in self.received()]
                self.assertNotIn("thread/resume", methods)
                self.assertNotIn("turn/start", methods)
                self.assertEqual(self.approvals, [])

    def test_preloaded_helpers_do_not_gain_parent_approval_or_admit_unrelated_actions(self):
        result = self.run_fixture("preload-child-approval", thread_id=THREAD)
        self.assertEqual(self.approvals, [])
        self.assertEqual(result.approval_events, ({"method": "mcp_elicitation", "mode": "openai_form",
                                                 "host_callback": False, "action": "cancel"},))
        error = self.assert_failure("preload-unknown-action", thread_id=THREAD)
        self.assertTrue(error.uncertain)
        self.assertEqual(error.notification_method, "item/completed")

    def test_historical_child_graph_alone_does_not_count_as_external_work(self):
        error = self.assert_failure("preload-capacity", thread_id=THREAD)
        self.assertTrue(error.capacity)
        self.assertFalse(error.uncertain)

    def test_child_work_during_resume_preserves_uncertainty_before_parent_turn(self):
        error = self.assert_failure("preload-early-child-unsafe-policy", thread_id=THREAD)
        self.assertTrue(error.uncertain)
        self.assertIsNone(error.turn_id)
        self.assertFalse(error.safe_stopped)
        self.assertNotIn("turn/start", [value["method"] for value in self.received()])

    def test_preexisting_active_or_unverified_child_keeps_browser_uncertain(self):
        for suffix in ("active-child", "unknown-status", "missing-status", "system-error"):
            with self.subTest(suffix=suffix):
                error = self.assert_failure("preload-" + suffix, thread_id=THREAD)
                self.assertTrue(error.uncertain)
                self.assertFalse(error.safe_stopped)
                self.assertIsNone(error.turn_id)
                methods = [value["method"] for value in self.received()]
                self.assertNotIn("thread/resume", methods)
                self.assertNotIn("turn/start", methods)

    def test_exact_eligible_elicitation_uses_host_policy(self):
        result = self.run_fixture("elicitation")
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(self.approvals[0]["turnId"], TURN)
        self.assertEqual(self.received()[-1], {"id": 77, "result": {
            "action": "accept", "content": {"approved": True}}})
        self.assertEqual(result.approval_events, ({"method": "mcp_elicitation", "mode": "openai_form",
                                                 "host_callback": True, "action": "accept"},))

    def test_unknown_forms_default_cancel_and_declines_keep_no_content(self):
        for answer, expected in [(None, "cancel"), ({"action": "accept"}, "cancel"),
                ({"action": "accept", "content": {}, "proof": "bad"}, "cancel"),
                ({"action": "accept", "content": {}, "_meta": {"persist": "always"}}, "cancel"),
                ({"action": "accept", "content": {}, "_meta": {"persist": "session", "approvals_reviewer": "user"}}, "cancel"),
                ({"action": "decline", "content": {"ignored": True}}, "decline")]:
            with self.subTest(answer=answer):
                result = self.run_fixture("elicitation", approve_request=lambda p: answer)
                self.assertEqual(self.received()[-1]["result"], {"action": expected, "content": None})
                self.assertEqual(result.approval_events[0]["action"], expected)

    def test_only_exact_host_session_metadata_is_forwarded(self):
        response = {"action": "accept", "content": {}, "_meta": {"persist": "session"}}
        self.run_fixture("elicitation", approve_request=lambda params: response)
        self.assertEqual(self.received()[-1]["result"], response)

    def test_device_verification_and_url_never_reach_callback(self):
        for mode in ("verification", "url"):
            with self.subTest(mode=mode):
                result = self.run_fixture(mode)
                self.assertEqual(self.approvals, [])
                self.assertEqual(self.received()[-1]["result"], {"action": "cancel", "content": None})
                self.assertEqual(result.approval_events, ({"method": "mcp_elicitation",
                    "mode": "user_verification" if mode == "verification" else "url",
                    "host_callback": False, "action": "cancel"},))

    def test_unknown_server_method_fails_closed_without_approval(self):
        result = self.run_fixture("unknown-request")
        self.assertEqual(self.approvals, [])
        self.assertEqual(self.received()[-1]["error"]["code"], -32601)
        self.assertEqual(result.approval_events, ({"method": "unsupported", "mode": "unsupported",
                                                 "host_callback": False, "action": "cancel"},))

    def test_approval_events_default_and_unknown_values_are_sanitized(self):
        fake = AppServerResult({}, THREAD, True, True, (), 0, 0)
        self.assertEqual(fake.approval_events, ())
        result = self.run_fixture("elicitation-unsupported-mode")
        self.assertEqual(result.approval_events, ({"method": "mcp_elicitation", "mode": "unsupported",
                                                 "host_callback": False, "action": "cancel"},))
        text = json.dumps(result.approval_events)
        for forbidden in ("private", "resume", "upload", "77", THREAD, TURN):
            self.assertNotIn(forbidden, text)

    def test_scoped_elicitation_requires_exact_turn_and_unique_request_id(self):
        for mode in ("elicitation-wrong-turn", "elicitation-no-turn", "elicitation-duplicate"):
            with self.subTest(mode=mode):
                self.approvals.clear()
                error = self.assert_failure(mode)
                self.assertTrue(error.uncertain)
                self.assertEqual(len(self.approvals), 1 if mode.endswith("duplicate") else 0)

    def test_pretool_capacity_only_complete_unmixed_failure_is_retryable(self):
        error = self.assert_failure("capacity")
        self.assertTrue(error.capacity)
        self.assertFalse(error.uncertain)
        for mode in ("capacity-after-tool", "capacity-mixed", "capacity-stderr", "capacity-truncated",
                     "capacity-conflicting-code"):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode)
                self.assertFalse(error.capacity)
                self.assertTrue(error.uncertain)

    def test_failed_browser_final_stream_and_turn_never_return_success(self):
        for mode in ("no-browser", "failed-browser", "browser-result-error", "bad-final",
                     "duplicate-final-json", "bad-json", "duplicate-response", "failed-turn",
                     "completed-with-error", "exit-error"):
            with self.subTest(mode=mode):
                self.assertTrue(self.assert_failure(mode).uncertain)

    def test_identity_and_restricted_policy_are_checked(self):
        error = self.assert_failure("wrong-thread", thread_id=THREAD)
        self.assertFalse(error.uncertain)
        self.assertFalse(self.assert_failure("unsafe-policy").uncertain)
        self.assertTrue(self.assert_failure("wrong-turn").uncertain)

    def test_explicit_turn_rejection_is_safe_with_private_diagnostics(self):
        error = self.assert_failure("reject-turn")
        self.assertFalse(error.uncertain)
        self.assertFalse(error.capacity or error.safe_stopped)
        self.assertEqual(error.reason, "protocol_rejected")
        self.assertEqual(error.protocol_method, "turn/start")
        self.assertEqual(error.protocol_code, -32602)
        self.assertEqual(error.protocol_error, {"code": -32602,
            "message": "private schema validation error", "data": {"field": "outputSchema"}})
        self.assertNotIn("private", repr(error))

    def test_resume_writer_rejection_never_sends_a_turn(self):
        error = self.assert_failure("resume-active-writer", thread_id=THREAD)
        self.assertFalse(error.uncertain or error.capacity or error.safe_stopped)
        self.assertEqual(error.thread_id, THREAD)
        self.assertIsNone(error.turn_id)
        self.assertEqual(error.protocol_method, "thread/resume")
        self.assertEqual(error.protocol_code, -32600)
        self.assertEqual(error.protocol_error, {"code": -32600,
            "message": f"thread {THREAD} already has an active writer"})
        self.assertEqual([value["method"] for value in self.received()],
                         ["initialize", "initialized", "thread/list", "thread/resume"])

    def test_rejection_after_observed_turn_and_lost_response_remain_uncertain(self):
        for mode in ("reject-after-start", "reject-after-tool", "lost-turn-response"):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode)
                self.assertTrue(error.uncertain)
                if mode == "lost-turn-response":
                    self.assertIsNone(error.protocol_error)
                else:
                    self.assertEqual(error.protocol_method, "turn/start")
                    self.assertEqual(error.turn_id, TURN)

    def test_timeout_and_shutdown_are_bounded(self):
        self.assertFalse(self.assert_failure("slow-init", timeout=.2).uncertain)
        self.assertTrue(self.assert_failure("slow-turn", timeout=1).uncertain)
        begin = time.monotonic()
        self.assertTrue(self.assert_failure("shutdown-hang").uncertain)
        self.assertLess(time.monotonic() - begin, 3)
        self.assertGreater(self.beats, 4)

    def test_shutdown_before_start_does_not_spawn_and_is_safe(self):
        self.closed.set()
        error = self.assert_failure("success")
        self.assertTrue(error.safe_stopped)
        self.assertFalse(error.uncertain)
        self.assertFalse((self.root / "received.jsonl").exists())

    def test_shutdown_during_turn_remains_uncertain(self):
        error = self.assert_failure("slow-turn", closed=lambda: (self.root / "turn-started").exists())
        self.assertTrue(error.uncertain)
        self.assertFalse(error.safe_stopped)

    def test_lost_lease_never_grants_approval_and_hides_error(self):
        def lost():
            raise RuntimeError("private lease token")
        error = self.assert_failure("elicitation", heartbeat=lost)
        self.assertFalse(error.uncertain)
        self.assertEqual(self.approvals, [])

    def test_authorization_callback_failure_is_safe_and_private(self):
        def fail(params):
            raise RuntimeError("private profile")
        self.assertTrue(self.assert_failure("elicitation", approve_request=fail).uncertain)

    def test_stream_line_and_total_limits(self):
        with patch("codex_appserver.MAX_LINE_BYTES", 2000):
            self.assertTrue(self.assert_failure("large-line").uncertain)
        with patch("codex_appserver.MAX_EVENT_BYTES", 100):
            self.assertFalse(self.assert_failure("success").uncertain)
        with patch("codex_appserver.MAX_RESULT_BYTES", 5):
            self.assertTrue(self.assert_failure("success").uncertain)

    def test_shutdown_terminates_child_process_group(self):
        self.assertTrue(self.assert_failure("child", closed=lambda: (self.root / "child-started").exists()).uncertain)
        time.sleep(1.1)
        self.assertFalse((self.root / "child-marker").exists())

    def test_reaped_process_group_eperm_keeps_original_error(self):
        with patch("codex_appserver.os.killpg", side_effect=PermissionError("denied")):
            error = self.assert_failure("no-browser")
        self.assertTrue(error.uncertain)
        self.assertEqual(str(error), "App-server did not verify a browser tool call.")

    def test_live_process_group_eperm_is_still_an_uncertain_failure(self):
        process = Mock(pid=42)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("fictional", 2)
        with patch("codex_appserver.os.killpg", side_effect=PermissionError("denied")):
            with self.assertRaises(AppServerError) as caught:
                _stop(process)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.reason, "cleanup_failed")

    def test_resume_without_browser_keeps_verified_turn_error(self):
        error = self.assert_failure("no-browser", thread_id=THREAD)
        self.assertTrue(error.uncertain)
        self.assertEqual(error.thread_id, THREAD)
        self.assertEqual(str(error), "App-server did not verify a browser tool call.")
        self.assertEqual(self.received()[3]["method"], "thread/resume")

    def test_resume_previous_turn_usage_is_passive_and_thread_fenced(self):
        result = self.run_fixture("historical-usage", thread_id=THREAD)
        self.assertEqual(result.result, {"ok": True})
        self.assertTrue(result.browser_verified)
        self.assertEqual(self.approvals, [])
        error = self.assert_failure("historical-usage-no-browser", thread_id=THREAD)
        self.assertEqual(str(error), "App-server did not verify a browser tool call.")
        error = self.assert_failure("historical-usage-wrong-thread", thread_id=THREAD)
        self.assertFalse(error.uncertain)
        self.assertEqual(str(error), "App-server returned a different thread identity.")

    def test_native_linked_child_notifications_do_not_replace_parent_evidence(self):
        for mode in ("child-events", "child-events-parent-link", "child-events-late-completion",
                     "child-events-status-before-link"):
            with self.subTest(mode=mode):
                result = self.run_fixture(mode)
                self.assertEqual(result.thread_id, THREAD)
                self.assertEqual(result.result, {"ok": True})
                self.assertTrue(result.turn_completed and result.browser_verified)
                self.assertEqual(result.approval_events, ())

    def test_child_tools_results_and_completion_cannot_satisfy_parent_contract(self):
        for mode, message in (("child-events-no-parent-browser", "browser tool call"),
                ("child-events-no-parent-final", "no final result"),
                ("child-events-no-parent-completion", "timed out"),
                ("child-events-active", "timed out"),
                ("child-events-active-status-idle", "timed out")):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode, timeout=1)
                self.assertTrue(error.uncertain)
                self.assertIn(message, str(error))

    def test_runtime_status_is_passive_before_helper_relationship_is_known(self):
        result = self.run_fixture("runtime-status", thread_id=THREAD)
        self.assertEqual(result.result, {"ok": True})
        self.assertEqual(result.tools, (("cua_repl", "js"),))
        self.assertEqual(result.approval_events, ())
        self.assertEqual(self.approvals, [])
        for mode, message in (("runtime-status-no-browser", "browser tool call"),
                ("runtime-status-no-final", "no final result"),
                ("runtime-status-no-completion", "timed out")):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode, thread_id=THREAD, timeout=1)
                self.assertTrue(error.uncertain)
                self.assertIn(message, str(error))

    def test_runtime_status_does_not_register_unknown_thread_for_actions(self):
        for mode in ("runtime-status-unlinked-tool", "runtime-status-unlinked-turn",
                     "runtime-status-unlinked-approval"):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode, thread_id=THREAD)
                self.assertTrue(error.uncertain)
                self.assertEqual(error.thread_id, THREAD)
                self.assertEqual(error.turn_id, TURN)
                self.assertEqual(self.approvals, [])

    def test_runtime_status_rejects_invalid_schema(self):
        for suffix in ("bad-id", "missing", "unknown", "bad-type", "missing-flags", "bad-flags"):
            with self.subTest(suffix=suffix):
                error = self.assert_failure("runtime-status-" + suffix, thread_id=THREAD)
                self.assertFalse(error.uncertain)
                self.assertEqual(error.notification_method, "thread/status/changed")
                self.assertEqual(str(error), "App-server returned an invalid thread status notification.")

    def test_child_elicitation_cannot_use_parent_policy(self):
        result = self.run_fixture("child-events-approval")
        self.assertEqual(self.approvals, [])
        self.assertEqual(result.approval_events, ({"method": "mcp_elicitation", "mode": "openai_form",
                                                 "host_callback": False, "action": "cancel"},))
        replies = [value for value in self.received() if value.get("id") == 77]
        self.assertEqual(replies, [{"id": 77, "result": {"action": "cancel", "content": None}}])

    def test_known_child_parent_interaction_does_not_register_parent_as_child(self):
        result = self.run_fixture("child-events-parent-callback")
        self.assertEqual(result.thread_id, THREAD)
        self.assertEqual(result.result, {"ok": True})
        self.assertTrue(result.browser_verified and result.turn_completed)
        self.assertEqual(result.approval_events, ())
        error = self.assert_failure("child-events-parent-callback-no-browser")
        self.assertTrue(error.uncertain)
        self.assertEqual(str(error), "App-server did not verify a browser tool call.")

    def test_parent_interaction_exception_requires_known_child_and_exact_kind(self):
        for mode in ("child-events-invalid-parent-spawn", "invalid-parent-self-interaction",
                     "unlinked-parent-callback"):
            with self.subTest(mode=mode):
                error = self.assert_failure(mode)
                self.assertTrue(error.uncertain)
                self.assertEqual(error.notification_method, "item/started")
                self.assertEqual(self.approvals, [])

    def test_unlinked_child_still_fails_with_fixed_notification_context(self):
        error = self.assert_failure("unlinked-child-events")
        self.assertTrue(error.uncertain)
        self.assertEqual(str(error), "App-server returned a different thread identity.")
        self.assertEqual(error.notification_method, "thread/started")
        error = self.assert_failure("wrong-turn")
        self.assertEqual(error.notification_method, "item/completed")
        self.assertEqual(error.thread_id, THREAD)
        self.assertEqual(error.turn_id, TURN)

    def inspect_fixture(self, mode, **overrides):
        (self.root / "scenario").write_text(mode)
        options = dict(binary=self.binary, cwd=self.root, thread_id=THREAD, turn_id=TURN,
                       closed=self.closed, timeout=4)
        options.update(overrides)
        return inspect_inactive_turn(**options)

    def test_inactive_turn_inspection_accepts_only_complete_passive_proof(self):
        for mode, status in (("inspect-empty", "interrupted"), ("inspect-passive", "interrupted"),
                             ("inspect-completed", "completed"), ("inspect-failed", "failed")):
            with self.subTest(mode=mode):
                proof = self.inspect_fixture(mode)
                self.assertEqual(proof["thread_id"], THREAD)
                self.assertEqual(proof["turn_id"], TURN)
                self.assertEqual(proof["turn_status"], status)
                self.assertEqual(proof["items_view"], "full")
                self.assertEqual(proof["stable_snapshots"], 2)
                self.assertEqual(proof["external_item_count"], 0)
                self.assertEqual(proof["child_count"], 0)
                self.assertEqual(proof["inspector_exit_code"], 0)
                self.assertEqual(proof["item_count"], 0 if mode == "inspect-empty" else 5)
                self.assertNotIn("private", json.dumps(proof))

    def test_inactive_turn_inspection_never_sends_mutating_rpc(self):
        self.assertIsNotNone(self.inspect_fixture("inspect-empty"))
        methods = [value["method"] for value in self.received()]
        self.assertEqual(methods, ["initialize", "initialized", "thread/read", "thread/turns/list",
                                   "thread/read", "thread/turns/list"])
        for value in self.received():
            if value["method"] == "thread/turns/list":
                self.assertEqual(value["params"], {"threadId": THREAD, "limit": 1,
                                                   "sortDirection": "desc", "itemsView": "full"})

    def test_inactive_turn_inspection_rejects_incomplete_or_external_work(self):
        for mode in ("inspect-active-thread", "inspect-active-turn", "inspect-tool", "inspect-subagent",
                     "inspect-unknown-item", "inspect-many-items", "inspect-duplicate-items", "inspect-partial",
                     "inspect-wrong-thread", "inspect-wrong-turn", "inspect-changed", "inspect-error",
                     "inspect-truncated", "inspect-missing-end"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.inspect_fixture(mode))

    def test_closed_inspector_returns_no_proof_without_spawning(self):
        self.closed.set()
        self.assertIsNone(self.inspect_fixture("inspect-empty"))
        self.assertFalse((self.root / "received.jsonl").exists())

    def test_inspection_warning_reports_only_exception_type(self):
        with self.assertLogs("codex_appserver", level="WARNING") as captured:
            self.assertIsNone(self.inspect_fixture("inspect-error"))
        self.assertEqual(captured.output, ["WARNING:codex_appserver:Inactive-turn inspection failed (AppServerError)."])
        self.assertNotIn("private", " ".join(captured.output))

    def transport(self, run_directory=None):
        return CodexTransport(self.binary, run_directory or self.root / "runs", cwd=self.root,
                              timeout=4, heartbeat_interval=.05, browser_transport="app-server")

    def lease(self):
        class Lease:
            task_id = str(uuid.uuid4())
            token = str(uuid.uuid4())
            def heartbeat(self):
                return True
            def checkpoint(self, value):
                return True
        return Lease()

    def test_transport_new_resume_receipts_and_restart(self):
        (self.root / "scenario").write_text("success")
        lease = self.lease()
        for _ in range(2):
            self.assertEqual(self.transport().run("application_prepare", "fixture", SCHEMA,
                                                  lease, browser=True), {"ok": True})
        receipts = [json.loads(path.read_text()) for path in (self.root / "runs").glob("*/receipt.json")]
        self.assertEqual(len(receipts), 2)
        self.assertEqual({v["resumed_thread_id"] for v in receipts}, {None, THREAD})
        for receipt in receipts:
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["transport"], "app-server")
            self.assertEqual(receipt["exit_code"], 0)
            self.assertTrue(receipt["turn_completed"] and receipt["browser_verified"])
            self.assertEqual(receipt["tools"], [{"server": "cua_repl", "tool": "js"}])
            self.assertNotIn("fixture", json.dumps(receipt))
        for path in (self.root / "runs").rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        methods = [value.get("method") for value in self.received()]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("thread/resume"), 1)

    def test_transport_capacity_preserves_previous_session_for_retry(self):
        (self.root / "scenario").write_text("success")
        lease = self.lease()
        self.transport().run("application_prepare", "fixture", SCHEMA, lease, browser=True)
        binding = self.root / "runs" / "_browser_sessions" / (lease.task_id + ".json")
        before = binding.read_bytes()
        (self.root / "scenario").write_text("capacity")
        with self.assertRaises(WorkerCapacityUnavailable):
            self.transport().run("application_prepare", "fixture", SCHEMA, lease, browser=True)
        self.assertEqual(binding.read_bytes(), before)
        (self.root / "scenario").write_text("success")
        self.assertEqual(self.transport().run("application_prepare", "fixture", SCHEMA,
                                              lease, browser=True), {"ok": True})

    def test_transport_writer_busy_preserves_both_bindings_and_resumes_same_thread(self):
        (self.root / "scenario").write_text("success")
        lease = self.lease()
        for role in ("application_prepare", "linkedin_session"):
            self.transport().run(role, "fixture", SCHEMA, lease, browser=True)
        directory = self.root / "runs" / "_browser_sessions"
        before = {path: path.read_bytes() for path in directory.glob("*.json")}
        calls = len(self.received())
        (self.root / "scenario").write_text("resume-active-writer")
        with self.assertRaises(BrowserRecoverable) as caught:
            self.transport().run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
        self.assertEqual(str(caught.exception), "browser_session_unavailable")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual([value["method"] for value in self.received()[calls:]],
                         ["initialize", "initialized", "thread/list", "thread/resume"])
        receipt, = [json.loads(path.read_text()) for path in
                    (self.root / "runs").glob("*/receipt.json")
                    if json.loads(path.read_text())["status"] == "failed"]
        self.assertTrue(receipt["failure"]["safe_before_turn"])
        self.assertEqual(receipt["resumed_thread_id"], THREAD)
        self.assertEqual(receipt["thread_id"], THREAD)
        self.assertFalse(receipt["turn_completed"] or receipt["browser_verified"])
        self.assertEqual(receipt["tools"], [])
        self.assertEqual(receipt["approval_events"], [])
        (self.root / "scenario").write_text("success")
        calls = len(self.received())
        self.assertEqual(self.transport().run("linkedin_session", "fixture", SCHEMA,
                                              lease, browser=True), {"ok": True})
        resumed = self.received()[calls:]
        self.assertEqual([value["method"] for value in resumed],
                         ["initialize", "initialized", "thread/list", "thread/resume", "turn/start"])
        self.assertEqual(resumed[3]["params"]["threadId"], THREAD)
        application = directory / (lease.task_id + ".json")
        self.assertEqual(application.read_bytes(), before[application])

    def test_transport_writer_recovery_requires_exact_safe_bound_rejection(self):
        diagnostic = {"code": -32600, "message": f"thread {THREAD} already has an active writer"}
        base = dict(thread_id=THREAD, reason="protocol_rejected", protocol_method="thread/resume",
                    protocol_code=-32600, protocol_error=diagnostic)
        variants = [
            {"thread_id": "33333333-3333-4333-8333-333333333333"},
            {"thread_id": None}, {"turn_id": TURN}, {"reason": "transport_failed"},
            {"protocol_method": "turn/start"}, {"protocol_code": -32602},
            {"protocol_code": "-32600"}, {"protocol_error": None},
            {"protocol_error": {**diagnostic, "code": -32602}},
            {"protocol_error": {**diagnostic, "message": "private unrelated protocol error"}},
            {"protocol_error": {**diagnostic, "message": diagnostic["message"] + "."}},
            {"protocol_error": {**diagnostic, "message": diagnostic["message"].replace(THREAD, TURN)}},
            {"uncertain": True},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                # Each independent native fault owns its own fixture connection.
                # A prior uncertain owner must not be cleared for the next case.
                transport = self.transport(self.root / ('fault-' + str(uuid.uuid4())))
                lease = self.lease()
                (self.root / "scenario").write_text("success")
                transport.run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
                failure = AppServerError("App-server rejected a protocol request.", **{**base, **changes})
                expected = BrowserWorkUncertain if failure.uncertain else CodexTransportError
                with patch("codex_appserver.run_appserver", side_effect=failure) as native:
                    with self.assertRaises(expected):
                        transport.run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
                    native.assert_called_once()
                if failure.uncertain:
                    with patch("codex_appserver.run_appserver") as native:
                        with self.assertRaises(BrowserWorkUncertain):
                            transport.run("linkedin_session", "fixture", SCHEMA, lease, browser=True)
                        native.assert_not_called()
        with patch("codex_appserver.run_appserver", side_effect=AppServerError(
                "App-server rejected a protocol request.", **base)):
            with self.assertRaises(CodexTransportError):
                self.transport().run("linkedin_session", "fixture", SCHEMA, self.lease(), browser=True)

    def test_transport_invalid_completed_contract_quarantines_without_replay(self):
        (self.root / "scenario").write_text("success")
        lease = self.lease()
        bad_schema = dict(SCHEMA, properties={"ok": {"type": "string"}})
        with self.assertRaises(BrowserWorkUncertain):
            self.transport().run("application_prepare", "fixture", bad_schema, lease, browser=True)
        before = (self.root / "received.jsonl").read_bytes()
        with self.assertRaises(BrowserWorkUncertain):
            self.transport().run("application_prepare", "fixture", SCHEMA, lease, browser=True)
        self.assertEqual((self.root / "received.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
