"""Opt-in genuine Docker/OpenAI A→B handoff smoke test. Never uses simulated workers.

Configure deploy/README.md, then run:
python3 scripts/live_smoke.py --run-live --data ./live-proof
This may incur organization API charges. Without --run-live, it explicitly skips.
"""
import argparse
import json
import os
import shutil
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.security import clean  # noqa: E402
from relay.config import load_env_file  # noqa: E402
from relay.store import Relay  # noqa: E402
from relay.workers import OrganizationApiCodexAdapter  # noqa: E402
from scripts.demo import exclusive_demo_directory  # noqa: E402

DEFAULT_OBJECTIVE=('Create text_tools.py with summarize_lines(text), returning a dictionary with the number of non-empty lines, '
                   'the longest non-empty line (first on ties), and the total word count. Handle empty and Unicode input. '
                   'Add at least four Python unittest cases and a short README usage example. '
                   'This is a two-phase task: first implement a useful initial part; leave meaningful remaining implementation and tests for the next worker.')

def readiness():
    """Read-only checks; never print environment values or make a provider request."""
    checks=[
        ('Organization API enabled (RELAY_ENABLE_ORG_API=1)',os.environ.get('RELAY_ENABLE_ORG_API')=='1'),
        ('Organization API key configured (RELAY_ORG_OPENAI_API_KEY)',bool(os.environ.get('RELAY_ORG_OPENAI_API_KEY'))),
        ('Inference proxy token configured (RELAY_CODEX_PROXY_TOKEN)',bool(os.environ.get('RELAY_CODEX_PROXY_TOKEN'))),
        ('Approved model configured (RELAY_CODEX_MODEL)',bool(os.environ.get('RELAY_CODEX_MODEL'))),
        ('Internal Docker network configured (RELAY_CODEX_NETWORK)',bool(os.environ.get('RELAY_CODEX_NETWORK'))),
        ('Non-root execution user',os.getuid()!=0),
        ('Docker CLI installed',shutil.which('docker') is not None),
    ]
    if checks[-1][1]:
        try:
            daemon=subprocess.run(['docker','info','--format','{{.ServerVersion}}'],capture_output=True,timeout=5)
            checks.append(('Docker daemon running',daemon.returncode==0))
        except (OSError,subprocess.SubprocessError): checks.append(('Docker daemon running',False))
    return checks

def preflight():
    missing=[label for label,ready in readiness() if not ready]
    if missing: raise RuntimeError('Live preflight blocked. Missing prerequisites:\n  - '+'\n  - '.join(missing)+'\nConfigure .env using .env.example, then use --env-file .env --check. See deploy/README.md.')
    worker={'id':'preflight','owner_type':'org','owner_id':'orgdemo','execution_mode':'organization_api','credential_ref':'env:RELAY_ORG_OPENAI_API_KEY'}
    adapter=OrganizationApiCodexAdapter(worker)
    health=adapter.health()
    if not health['available']: raise RuntimeError('Live preflight blocked: '+health['reason'])
    image=adapter._configuration()['image']
    result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                           '--pids-limit=32','--memory=256m','--cpus=1',image,'codex','--version'],capture_output=True,text=True,timeout=30,check=True)
    version=clean(result.stdout.strip())
    if 'codex-cli 0.163.0' not in version: raise RuntimeError('Execution image CLI does not match the verified 0.163.0 contract; rebuild the reviewed image')
    print('Live preflight: '+version,flush=True)
    return adapter._configuration()['timeout']

def full_events(relay,user,conversation):
    result=[]; after=0
    while True:
        page=relay.events(user,conversation,after,100); result.extend(page['events'])
        if len(page['events'])<100:return result
        after=page['next_after']

def wait_for(relay,task_id,conversation,states,timeout,after=0):
    deadline=time.monotonic()+timeout; cursor=after
    while time.monotonic()<deadline:
        with relay.lock:
            task=relay.task('alice',task_id)
            events=relay.events('alice',conversation,cursor,100)['events']
            for event in events:
                cursor=event['sequence']
                if event['event_type'] in ('response','tool_invocation','tool_output','runtime_output','error','provider_error','pause_boundary','handoff','state'):
                    print(f"[{event['sequence']} / {event['worker_id']} / {event['event_type']}] {clean(event['content'])[:3000]}",flush=True)
            if task_id not in relay.jobs and task['state'] in states:return task
        time.sleep(.2)
    raise RuntimeError('Smoke deadline exceeded. Workspace retained; inspect its task state before retrying.')

def run(data,objective):
    timeout=preflight()
    with exclusive_demo_directory(data):
        relay=Relay(data)
        try:
            project=relay.mutate('admin','projects',{'name':'Genuine Codex handoff smoke'})['id']
            relay.mutate('admin','membership',{'project_id':project,'user_id':'alice'})
            conversation=relay.detail('alice',project)['conversation']['id']
            workers=[]
            for name in ('Organization API A','Organization API B'):
                worker=relay.mutate('admin','workers/enroll',{'display_name':name,'owner_type':'org','execution_mode':'organization_api','opt_in':True,'project_ids':[project]})
                if not worker['eligible']:raise RuntimeError(worker['eligibility_reason'])
                workers.append(worker['id'])
            report={'project_id':project,'conversation_id':conversation,'workers':workers,'mode':'organization_api','live_verified':False}
            report_path=Path(data)/'live-smoke-report.json';report_path.write_text(json.dumps(report,indent=2))
            task=relay.mutate('alice','prompt',{'conversation_id':conversation,'worker_id':workers[0],'execution_mode':'organization_api','prompt':objective,'pause_after_turn':True})
            report['task_id']=task['id'];report_path.write_text(json.dumps(report,indent=2))
            first=wait_for(relay,task['id'],conversation,{'handoff_pending','failed','cancelled'},timeout+45)
            if first['state']!='handoff_pending':raise RuntimeError('Worker A did not reach its safe handoff boundary')
            phase=relay.detail('alice',project)
            if not json.loads(first['progress_json']).get('pending_steps'):raise RuntimeError('Worker A did not report substantive remaining work; this run does not prove continuation')
            if not phase['workspace'].get('task_diff'):raise RuntimeError('Worker A produced no reviewable code changes')
            if not phase['contexts'] or '.relay/HANDOFF.md' not in phase['workspace']['files']:raise RuntimeError('Missing saved context or handoff artifact')
            print('MANUAL HANDOFF: A has stopped and checkpointed. Starting distinct worker B in the same conversation.',flush=True)
            after=full_events(relay,'alice',conversation)[-1]['sequence']
            relay.mutate('alice','switch',{'task_id':task['id'],'generation':first['generation'],'target_worker_id':workers[1],'reason_code':'manual_switch'})
            final=wait_for(relay,task['id'],conversation,{'completed','failed','handoff_pending','cancelled'},timeout+45,after)
            if final['state']!='completed':raise RuntimeError('Worker B did not complete; inspect errors/pending steps. No simulated fallback was attempted.')
            detail=relay.detail('alice',project); events=full_events(relay,'alice',conversation)
            if final['conversation_id']!=conversation:raise RuntimeError('Conversation identity changed')
            runs=detail['execution_runs']
            if len(runs)!=2 or any(r['exit_code']!=0 or not r['native_thread_id'] for r in runs):raise RuntimeError('Both Codex executions must supply a successful real exit and native thread ID')
            if runs[0]['native_thread_id']==runs[1]['native_thread_id']:raise RuntimeError('Expected distinct native execution threads')
            progress=json.loads(final['progress_json'])
            if progress.get('pending_steps') or not progress.get('test_results'):raise RuntimeError('Final result lacks completion/test evidence')
            test_items=[json.loads(e['metadata']).get('provider_item_id') for e in events if e['worker_id']==workers[1] and e['event_type']=='tool_invocation' and re.search(r'test|pytest|unittest|spec',e['content'],re.I)]
            if not any(e['worker_id']==workers[1] and e['event_type']=='tool_output' and json.loads(e['metadata']).get('provider_item_id') in test_items and json.loads(e['metadata']).get('exit_code')==0 for e in events):
                raise RuntimeError('Worker B must actually run a successful test command, not only describe tests')
            if relay.rows('SELECT * FROM workspace_leases WHERE project_id=?',(project,)):raise RuntimeError('Writer lease was not released')
            before_runs=detail['execution_runs']; before_context=detail['contexts']; before_code=detail['workspace']['files']
            relay.close(); relay=Relay(data)
            restored=relay.detail('alice',project)
            if restored['execution_runs']!=before_runs or restored['contexts']!=before_context or restored['workspace']['files']!=before_code or full_events(relay,'alice',conversation)!=events:
                raise RuntimeError('Restart persistence check failed')
            report.update({'live_verified':True,'task_id':task['id'],'checkpoint':restored['workspace']['checkpoint'],'execution_runs':before_runs,'handoffs':restored['handoffs'],'test_results':progress['test_results']})
            report_path.write_text(json.dumps(report,indent=2))
            print('PASS: genuine organization API A→B handoff, authentic tests, same conversation and restart persistence.',flush=True)
            print(f'Workspace: {Path(data).resolve()/"workspaces"/project}\nReport: {report_path.resolve()}',flush=True)
        finally:relay.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,help='Explicit Relay environment file; shell exports take precedence')
    parser.add_argument('--check',action='store_true',help='Check prerequisites only; never run Codex or call OpenAI')
    parser.add_argument('--run-live',action='store_true',help='Explicitly authorize a genuine organization API smoke run')
    parser.add_argument('--data',default='live-proof',help='Dedicated persistent directory; cannot be shared with a running server')
    parser.add_argument('--prompt-file',type=Path,help='Optional unseen programming objective; include meaningful tests and two phases')
    args=parser.parse_args()
    try:
        if args.env_file: load_env_file(args.env_file)
    except (ValueError,OSError) as exc:
        parser.exit(2,clean(str(exc))+'\n')
    if args.check:
        checks=readiness()
        for label,ready in checks: print(('PASS: ' if ready else 'MISSING: ')+label)
        if not all(ready for _,ready in checks): parser.exit(2,'Live execution is not ready. See deploy/README.md; no provider request made.\n')
        worker={'id':'preflight','owner_type':'org','execution_mode':'organization_api','credential_ref':'env:RELAY_ORG_OPENAI_API_KEY'}
        health=OrganizationApiCodexAdapter(worker).health()
        if not health['available']: parser.exit(2,'Runtime not ready: '+health['reason']+'\n')
        print('PASS: image and internal network present. This does not verify API authorization or model access. No provider request made.')
        return
    if not args.run_live:
        print('SKIP: real Docker/OpenAI execution requires --run-live and configured organization API authorization. No provider call made.')
        return
    try:run(args.data,args.prompt_file.read_text() if args.prompt_file else DEFAULT_OBJECTIVE)
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as exc:
        print('BLOCKED/FAILED: '+clean(str(exc)),file=sys.stderr);raise SystemExit(2) from None

if __name__=='__main__':main()
