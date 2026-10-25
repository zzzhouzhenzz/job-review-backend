"""Operator-only harmless native affinity probe. Never reads the application DB.

Concurrent fixture requests pass through one private ChromeAccessGate. Only
the admitted request starts a native browser turn. This cannot enable
production dispatch, install a proof, or submit forms.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from codex_runtime import load_runtime
from codex_appserver import run_appserver
from codex_transport import _private_write, validate_result
from application_pool_proof import collect_identity, fingerprint, PROOF_VERSION, GUARANTEE, CHROME_ACCESS
from chrome_access_gate import ChromeAccessGate


def stamp(): return datetime.now(timezone.utc).isoformat()
def write(path,value): _private_write(path,json.dumps(value,sort_keys=True))
def ref(path): return {'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

STAGES = ('browser_before', 'session_only', 'peer_work', 'browser_after')


def stage_schedule(workers):
    """The third worker has no native turn while its two peers continue."""
    return [(stage, [worker for worker in workers
                     if stage != 'peer_work' or worker['worker_id'] in {'apply-1', 'apply-2'}])
            for stage in STAGES]


def run_stage(stage, workers, turn):
    """Submit concurrent requests; the shared gate admits browser turns."""
    failures = []
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        for future in [pool.submit(turn, worker, stage) for worker in workers]:
            try:
                future.result()
            except Exception as error:
                failures.append(str(error))
    return failures


class FixtureGatePhase:
    """Fixture leases have no DB. The barrier proves all requests are queued."""
    def __init__(self, gate: ChromeAccessGate, stage, workers, closed, *, barrier_timeout=30):
        if stage not in {'browser_before', 'peer_work', 'browser_after'}:
            raise ValueError('Only browser phases use the Chrome gate.')
        self.gate, self.stage, self.closed = gate, stage, closed
        self.requested_at, self.record = {}, None
        self.guard = threading.Lock()
        self.barrier = threading.Barrier(len(workers), action=self._queued, timeout=barrier_timeout)

    def _queued(self):
        self.record = {'version': 1, 'stage': self.stage,
            'gate_directory': str(self.gate.directory.resolve()),
            'requested_at': dict(self.requested_at), 'queued_at': stamp(),
            'queued': self.gate.snapshot()}

    @contextmanager
    def dispatch(self, worker, run):
        ticket = None
        def stage(value):
            if value == 'waiting_for_browser':
                self.barrier.wait()
        lease = SimpleNamespace(task_id=worker['task_id'], application_worker_id=worker['worker_id'],
            token=str(uuid.uuid4()), heartbeat=lambda: True, stage=stage)
        with self.guard:
            self.requested_at[worker['worker_id']] = stamp()
        try:
            ticket = self.gate.acquire(lease, 'application_prepare', self.closed, .2)
            ticket.native_started(run)
            yield ticket
            if not ticket.safe_release:
                raise RuntimeError('Native fixture completion was not verified.')
        except BaseException:
            self.closed.set()
            self.barrier.abort()
            raise
        finally:
            if ticket is not None:
                try:
                    ticket.finish(safe=ticket.safe_release)
                except BaseException:
                    self.closed.set()
                    self.barrier.abort()
                    raise

    def evidence(self):
        return {**(self.record or {'version': 1, 'stage': self.stage,
                                  'requested_at': dict(self.requested_at)}),
                'finished_at': stamp(), 'final': self.gate.snapshot()}


def schema(browser=False):
    props={'worker_id':{'type':'string'},'task_id':{'type':'string'}}
    if browser:
        props.update({key:{'type':'string'} for key in ('tab_id','status','detail')})
        props.update({key:{'type':'array','items':{'type':'string'}} for key in ('owned_tab_ids','handoff_tab_ids')})
    return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}


def probe_prompt(worker, stage, *, resume=False, create_new=False, replace_missing=False, approval_text=None):
    """Scope every browser turn to the caller's single harmless fixture tab."""
    if stage not in STAGES:
        raise ValueError('Unknown fixture stage.')
    if stage == 'peer_work' and worker['worker_id'] == 'apply-3':
        raise ValueError('The third worker must remain idle during peer work.')
    if stage == 'session_only':
        return (f"Tool-free native resume probe. Do not use any tool, browser, command, files, or external action. "
            f"Return exactly the provided worker_id={worker['worker_id']} and task_id={worker['task_id']} in the output schema.")
    if stage == 'browser_before':
        if resume == create_new:
            raise ValueError('Choose exact fixture resumption or explicit fresh fixture creation.')
        if replace_missing and (not resume or not approval_text):
            raise ValueError('Fixture replacement requires resumed exact identities and renewed approval.')
    instructions=(f"This is an explicitly authorized harmless Job Review worker-pool verification, not a job application. "
        f"Your worker_id is {worker['worker_id']} and task_id is {worker['task_id']}. "
        "The host runs one Chrome connection at a time. This turn is your exclusive browser phase. "
        "Do not inspect, claim, navigate, close or act on any preexisting user/employer tab. Do not read application files, use shell commands, spawn helpers, or contact anyone. "
        "Use only supported Chrome CUA APIs and the documentation returned by the API. If any tool approval is denied, stop and report it; never retry through another path. "
        "Do not invent or infer proof. Return exact observed tab IDs and handoff evidence. ")
    if approval_text:
        instructions+=("NEW HUMAN APPROVAL FOR THIS HARMLESS FIXTURE CHECK: "
            f"The user replied verbatim: {json.dumps(approval_text)}. "
            "This fresh approval covers this worker's exact https://example.com fixture check, including required origin access through the supported CUA entry point, current-state reads, and markHandoff. "
            "It does not permit employer actions, other tabs, changing security settings, or bypassing a new denial. If any NEW approval denial occurs, stop and report it without retry. ")
    if stage == 'browser_before' and (create_new or replace_missing):
        if replace_missing:
            instructions+=(f"The operator confirmed your old harmless fixture tab {worker['tab_id']} is absent. "
                "The explicit replacement flag and fresh approval permit replacing only this missing fixture in the SAME existing native session. ")
        else:
            instructions+="The operator explicitly enabled creation of one new harmless fixture with --create-new-fixtures. "
        return instructions+(f"Create exactly ONE new Chrome tab at https://example.com/?job-review-pool-fixture={worker['task_id']} "
            f"with a short session name '🧪 Pool {worker['worker_id']}'. Start with the documented createBrowserTab entry point. "
            "Read its current state to confirm it is your new sentinel. Preserve it with the documented markHandoff operation before ending this turn. "
            "Return status='verified' only after that succeeds, owned_tab_ids and handoff_tab_ids each containing exactly this one tab. "
            "If blocked, return status='blocked', a precise detail and only actual IDs (empty lists when none); do not create an additional tab or fabricate handoff success.")
    purpose = ("Continue your own fixture while the third worker stays idle. " if stage == 'peer_work' else
               "Recheck your own preserved fixture after the prior probe phase. ")
    return instructions+purpose+(f"Use only your exact harmless fixture tab {worker['tab_id']}. Its URL must contain {worker['task_id']}. "
        "Use the supported getTab entry point and read its current state. Do not create, replace or close a tab. "
        "Confirm the existing fixture contents and then mark this same tab for handoff before ending the turn. "
        "Return status='verified' only after the exact tab, current contents and handoff succeed. Return owned_tab_ids and handoff_tab_ids each containing only this exact observed tab ID. "
        "If the tab is unavailable or a tool denies access, stop and return status='blocked' with the exact detail. An old saved tab ID alone is not evidence of preservation.")


def validate_observation(worker, stage, result, *, replace_missing=False):
    """Reject an invalid current turn before the host starts another phase."""
    browser = stage != 'session_only'
    value = result.result
    if (not result.turn_completed or type(result.exit_code) is not int or result.exit_code != 0
            or result.browser_verified is not browser or not browser and result.tools):
        raise RuntimeError('Native turn did not verify the required browser or tool-free mode.')
    if (value['worker_id'] != worker['worker_id'] or value['task_id'] != worker['task_id']
            or worker.get('thread_id') and result.thread_id != worker['thread_id']):
        raise RuntimeError('Native result identity mismatch')
    if browser:
        tab = value['tab_id']
        if (value['status'] != 'verified' or not tab or value['owned_tab_ids'] != [tab]
                or value['handoff_tab_ids'] != [tab]):
            raise RuntimeError(f"{worker['worker_id']} {stage} did not verify its own tab; inspect private result")
        if worker.get('tab_id') and tab != worker['tab_id'] and not (stage == 'browser_before' and replace_missing):
            raise RuntimeError('A fixture tab changed without explicit replacement authorization.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--output-directory',type=Path,required=True)
    parser.add_argument('--resume-directory',type=Path,help='Prior owned fixture identities; never employer sessions.')
    parser.add_argument('--create-new-fixtures',action='store_true',
                        help='Explicitly create one new harmless fixture tab per worker.')
    parser.add_argument('--approval-text',help='Verbatim new human approval for the existing harmless fixture checks.')
    parser.add_argument('--replace-missing-worker',action='append',default=[],
                        choices=('apply-1','apply-2','apply-3'),
                        help='Operator-confirmed missing harmless fixture to replace in the same native session.')
    args=parser.parse_args()
    if bool(args.resume_directory) == args.create_new_fixtures:
        raise ValueError('Choose --resume-directory or --create-new-fixtures, exclusively.')
    if args.replace_missing_worker and (not args.resume_directory or not args.approval_text):
        raise ValueError('Fixture replacement requires resumed exact identities and renewed approval.')
    root=args.output_directory.resolve()
    # Transport construction creates gate metadata. Reject a production path
    # before loading any runtime; the operator supplies a dedicated fixture config.
    config_path=args.runtime.resolve()
    config=json.loads(config_path.read_text())
    run_directory=Path(config['run_directory']).expanduser()
    if not run_directory.is_absolute(): run_directory=config_path.parent/run_directory
    if run_directory.resolve()!=root:
        raise ValueError('Probe runtime run_directory must equal the new output directory.')
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    # Loading configuration validates inputs but starts no service or job.
    runtime=load_runtime(args.runtime)
    transport=runtime.transport
    gate=transport.chrome_access_gate
    if gate.directory.resolve()!=root/'_chrome_access':
        runtime.close()
        raise ValueError('Probe requires its one private output gate.')
    identity=collect_identity(transport,runtime.pool_configuration_identity(),codex_bundle=Path('/Applications/ChatGPT.app'))
    write(root/'identity.json',identity)
    closed=threading.Event()
    gate_evidence={}
    phase=None
    workers=[{'worker_id':f'apply-{n}','task_id':str(uuid.uuid4())} for n in (1,2,3)]
    if args.resume_directory:
        previous=json.loads((args.resume_directory/'browser_before-workers.json').read_text())
        workers=[{key:w[key] for key in ('worker_id','task_id','thread_id','tab_id')} for w in previous]
        if len(workers)!=3 or {w['worker_id'] for w in workers}!={'apply-1','apply-2','apply-3'}:
            raise ValueError('Invalid fixture inventory')
    write(root/'workers.json',workers)
    replacements=[]
    if args.approval_text:
        write(root/'user-approval.json',{'text':args.approval_text,'recorded_at':stamp(),
            'scope':'Supported Chrome checks on each worker own example.com fixture tab, including required origin access, handoff and tool-free native resumption. Fresh creation and missing fixture replacement require their explicit flags.',
            'workers':workers})
    def turn(worker,stage):
        browser=stage!='session_only'
        run=root/(stage+'-'+str(uuid.uuid4()));run.mkdir(mode=0o700)
        prompt=probe_prompt(worker,stage,resume=bool(args.resume_directory),
            create_new=args.create_new_fixtures,replace_missing=worker['worker_id'] in args.replace_missing_worker,
            approval_text=args.approval_text)
        contract=schema(browser);write(run/'schema.json',contract);_private_write(run/'prompt.txt',prompt)
        started=None;finished=None;result=None;error=None;ticket=None;access=None
        def receipt():
            return {'status':'completed' if result and not error else 'failed','transport':'app-server',
                'application_worker_id':worker['worker_id'],'task_id':worker['task_id'],
                'thread_id':result.thread_id if result else (error or {}).get('thread_id'),
                'resumed_thread_id':worker.get('thread_id'),
                'turn_id':result.turn_id if result else (error or {}).get('turn_id'),
                'turn_completed':bool(result and result.turn_completed),'exit_code':result.exit_code if result else None,
                'browser':browser,'browser_verified':bool(result and result.browser_verified),'session_worker':not browser,
                'tools':[{'server':s,'tool':t} for s,t in sorted(result.tools)] if result else [],
                'approval_events':list(result.approval_events) if result else [],'failure':error,
                'started_at':started,'finished_at':finished, **({'chrome_access':access} if access else {})}
        try:
            with phase.dispatch(worker,run) if browser else nullcontext(None) as ticket:
                started=stamp()
                result=run_appserver(binary=transport.binary,cwd=transport.cwd,prompt=prompt,schema=contract,
                    thread_id=worker.get('thread_id'),timeout=transport.timeout,
                    heartbeat_interval=transport.heartbeat_interval,heartbeat=lambda:True,closed=closed,
                    approve_request=lambda request:{'action':'cancel','content':None},
                    thread_name='Job Review pool proof '+worker['worker_id'],require_browser=browser)
                validate_result(result.result,contract)
                validate_observation(worker,stage,result,
                                     replace_missing=worker['worker_id'] in args.replace_missing_worker)
                write(run/'result.json',result.result)
                finished=stamp()
                if ticket:
                    access={**ticket.receipt(),'enqueued_at':ticket.record['enqueued_at'],
                            'release_requested_at':stamp()}
                write(run/'receipt.json',receipt())
                # Only the complete, validated and persisted handoff permits release.
                if ticket: ticket.verified_terminal()
        except Exception as exc:
            closed.set()
            if phase: phase.barrier.abort()
            error={'type':type(exc).__name__,'message':str(exc),'reason':getattr(exc,'reason',None),
                   'thread_id':getattr(exc,'thread_id',None),'turn_id':getattr(exc,'turn_id',None),
                   'protocol_method':getattr(exc,'protocol_method',None),'protocol_error':getattr(exc,'protocol_error',None)}
            finished=stamp()
            if ticket:
                access={**ticket.receipt(),'enqueued_at':ticket.record['enqueued_at']}
            write(run/'error.json',error)
            write(run/'receipt.json',receipt())
        print(json.dumps({'worker':worker['worker_id'],'stage':stage,'status':receipt()['status'],
            'run':str(run),'reason':error.get('reason') if error else result.result.get('status','completed')}),flush=True)
        if error: raise RuntimeError(f"{worker['worker_id']} {stage} did not complete; inspect private evidence")
        worker[stage]={'started_at':started,'finished_at':finished,'receipt':ref(run/'receipt.json'),'result':ref(run/'result.json')}
        if stage=='browser_before':
            if worker['worker_id'] in args.replace_missing_worker:
                replacements.append({'worker_id':worker['worker_id'],'thread_id':result.thread_id,
                    'old_missing_tab_id':worker['tab_id'],'new_tab_id':result.result['tab_id'],
                    'receipt':ref(run/'receipt.json'),'result':ref(run/'result.json')})
            worker.update(thread_id=result.thread_id,tab_id=result.result['tab_id'])
    try:
        for stage, active_workers in stage_schedule(workers):
            phase=FixtureGatePhase(gate,stage,active_workers,closed) if stage!='session_only' else None
            failures=run_stage(stage,active_workers,turn)
            if phase:
                gate_path=root/(stage+'-chrome-access.json')
                write(gate_path,phase.evidence())
                gate_evidence[stage]=ref(gate_path)
            write(root/(stage+'-workers.json'),workers)
            if stage=='browser_before':write(root/'tab-replacements.json',replacements)
            if failures: raise RuntimeError('; '.join(failures))
        # Host validates all real evidence; no production setting is installed.
        from application_pool_proof import _verify
        proof={'version':PROOF_VERSION,'guarantee':GUARANTEE,'chrome_access':dict(CHROME_ACCESS),
               'identity':identity,'workers':workers,'gate_evidence':gate_evidence}
        _verify(proof,identity)
        write(root/'proof.json',proof)
        print(json.dumps({'proof':str(root/'proof.json'),'identity_digest':fingerprint(identity)}),flush=True)
    finally:
        runtime.close()

if __name__=='__main__':main()
