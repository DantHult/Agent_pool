"""Opt-in REAL owner-authenticated dashboard A → B proof on disposable files.

Uses existing Codex logins unchanged. No provider credentials are read/copied.
Playwright is an optional test dependency; the application remains stdlib-only.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.accounts import InviteIdentityProvider  # noqa: E402
from relay.config import load_env_file  # noqa: E402
from relay.native import session_for,close_native_sessions  # noqa: E402
from relay.server import make_server  # noqa: E402
from relay.security import clean  # noqa: E402
from relay.store import Relay  # noqa: E402

OBJECTIVE=('Create word_stats.py with summarize_words(text), returning word_count, unique_word_count (case insensitive), '
           'and longest_word (first on ties). Words are whitespace-separated. Handle empty and Unicode input. '
           'Add at least four Python unittest cases and a short README example. This is a two-phase handoff: '
           'first implement a useful initial part and leave meaningful implementation and tests for the successor.')


def run(data,chrome,objective):
    from playwright.sync_api import sync_playwright
    directory=Path(data)
    if (directory/'relay.sqlite').exists():raise ValueError('Use a fresh dedicated native proof directory; prior evidence is never overwritten')
    relay=Relay(directory,demo=False)
    password=secrets.token_urlsafe(24);email='local-owner@relay.invalid'
    owner=InviteIdentityProvider(relay).bootstrap(email,'Native Dashboard Owner',password,local_owner=True)['id'];relay.close()
    server=None;thread=None;report={'backend':'native','surface':'authenticated-local-owner-dashboard','live_verified':False,'stage':'startup'}
    report_path=directory/'local-native-dashboard-report.json'
    def save():report_path.write_text(json.dumps(report,indent=2))
    save()
    try:
        server=make_server(directory,0,'local-owner');origin=server.public_url
        with server.relay.lock:project=server.relay.mutate(owner,'projects',{'name':'Disposable real native dashboard proof'})['id']
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=chrome,headless=True)
            context=browser.new_context(viewport={'width':1500,'height':1100});page=context.new_page()
            page.goto(origin);page.locator('#accountEmail').fill(email);page.locator('#accountPassword').fill(password)
            page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function("() => !document.body.classList.contains('signed-out')")
            page.wait_for_function("() => document.querySelectorAll('[data-native-profile]').length === 3")
            page.locator('[data-pane=agents]').click()
            with server.relay.lock:profiles=server.relay.native_profiles(owner,project)['profiles']
            report.update({'project_id':project,'owner_authenticated':True,'stage':'profile-preflight'})
            for profile in ('a','b'):
                status=next(p for p in profiles if p['profile']==profile)
                print('Profile '+profile+': '+status['status'],flush=True)
                if not status['available']:raise RuntimeError('Native profile '+profile+' is unavailable: '+status['reason'])
            identities=[session_for(p).account_hash for p in ('a','b')]
            if not all(identities) or identities[0]==identities[1]:raise RuntimeError('Live account-continuation proof requires two distinct existing ChatGPT accounts')
            report['distinct_accounts']=True;save()
            page.locator('[data-native-profile=a]').click()
            page.wait_for_function("() => document.getElementById('startWorker').value !== ''")
            page.locator('#prompt').fill(objective);page.locator('.run-options summary').click();page.locator('#pauseAfterTurn').check();page.locator('#handoffPhases').select_option('2');page.locator('.run-options summary').click()
            report['stage']='native-A';save();page.locator('.send').click()
            print('REAL NATIVE A: owner submitted the objective through the dashboard.',flush=True)
            page.wait_for_function("() => document.getElementById('taskState').textContent === 'handoff_pending'",timeout=240000)
            with server.relay.lock:
                first=server.relay.detail(owner,project);task=first['tasks'][0]
                progress=json.loads(task['progress_json'])
                if not progress.get('pending_steps') or not first['workspace'].get('task_diff'):raise RuntimeError('A did not produce genuine unfinished work and a saved project diff')
            conversation=page.locator('#conversationId').inner_text();report.update({'conversation_id':conversation,'task_id':task['id'],'stage':'owner-handoff'})
            save()
            page.locator('[data-native-profile=b]').click()
            page.wait_for_function("() => document.getElementById('switchTarget').value !== ''")
            with server.relay.lock:
                if len(server.relay.detail(owner,project)['execution_runs'])!=1:raise RuntimeError('Preparing a successor unexpectedly executed it')
            report['stage']='native-B';save();page.locator('#switch').click()
            print('REAL NATIVE B: owner explicitly requested the handoff through the dashboard.',flush=True)
            page.wait_for_function("() => document.getElementById('taskState').textContent === 'completed'",timeout=240000)
            with server.relay.lock:
                detail=server.relay.detail(owner,project);events=[];after=0
                while True:
                    batch=server.relay.events(owner,conversation,after,100)['events'];events.extend(batch)
                    if len(batch)<100:break
                    after=batch[-1]['sequence']
                runs=detail['execution_runs'];handoffs=detail['handoffs'];final=detail['tasks'][0]
                if len(runs)!=2 or any(r['state']!='completed' or r['execution_mode']!='native' or r['exit_code']!=0 for r in runs):raise RuntimeError('Two genuinely completed native runs were not verified')
                if len({r['native_thread_id'] for r in runs})!=2:raise RuntimeError('Native threads were not distinct')
                if len(handoffs)!=1 or handoffs[0]['status']!='resumed' or not handoffs[0]['checkpoint']:raise RuntimeError('Checkpointed A to B handoff was not verified')
                for run in runs:
                    if not any(e['worker_id']==run['worker_id'] and e['event_type']=='native_patch' and json.loads(e['metadata']).get('patch_bytes',0)>0 for e in events):raise RuntimeError('A native worker made no verified project contribution')
                tests=[e for e in events if e['worker_id']==runs[1]['worker_id'] and e['event_type']=='tool_output' and json.loads(e['metadata']).get('exit_code')==0 and ('Ran ' in e['content'] and 'tests' in e['content'] and 'OK' in e['content'])]
                if not tests or json.loads(final['progress_json']).get('pending_steps'):raise RuntimeError('B did not finish and run successful Python tests')
                if server.relay.rows('SELECT * FROM workspace_leases'):raise RuntimeError('A writer lease remained after completion')
            page.reload();page.wait_for_function("() => document.getElementById('taskState').textContent === 'completed'")
            if page.locator('#conversationId').inner_text()!=conversation or 'NATIVE LOCAL' not in page.locator('#timeline').inner_text():raise RuntimeError('Reload lost conversation identity or native attribution')
            port=server.server_port;server.shutdown();server.server_close();thread.join(2);server=None
            server=make_server(directory,port,'local-owner',origin)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            page.reload();page.wait_for_function("() => document.getElementById('taskState').textContent === 'completed'")
            with server.relay.lock:
                restored=server.relay.detail(owner,project)
                if restored['execution_runs']!=runs or restored['checkpoints']!=detail['checkpoints'] or restored['workspace']['files']!=detail['workspace']['files'] or restored['contexts']!=detail['contexts']:raise RuntimeError('Native dashboard restart persistence failed')
                restored_events=[];after=0
                while True:
                    batch=server.relay.events(owner,conversation,after,100)['events'];restored_events.extend(batch)
                    if len(batch)<100:break
                    after=batch[-1]['sequence']
                if restored_events!=events:raise RuntimeError('Persisted transcript changed after restart')
            page.locator('[data-pane=agents]').click();page.locator('.workspace-records summary').click();page.locator('[data-tab=checkpoints]').click();page.screenshot(path=str(directory/'native-dashboard-proof.png'),full_page=True)
            report.update({'live_verified':True,'stage':'verified','execution_runs':runs,'handoffs':handoffs,'checkpoint':detail['workspace']['checkpoint'],'restart_persistence':True,'browser_reload_persistence':True,'test_output':tests[-1]['content'],'contexts_verified':len(detail['contexts']),'event_count':len(events)})
            save();context.close();browser.close()
        print('PASS: REAL NATIVE LOCAL dashboard A → B, owner authentication, genuine coding/tests, checkpointed explicit handoff, browser reload and backend restart persistence.',flush=True)
        print('Proof: '+str(report_path.resolve()),flush=True)
    except Exception as exc:
        report['failure_type']=type(exc).__name__
        if type(exc) in (RuntimeError,ValueError):report['failure_message']=clean(str(exc))[:1000]
        save()
        # Browser exceptions may include form input; do not echo them or secrets.
        print('BLOCKED/FAILED: '+report['stage']+' ('+type(exc).__name__+'). Inspect the private proof and persisted task events.',flush=True)
        if report.get('failure_message'):print(report['failure_message'],flush=True)
        raise SystemExit(2) from None
    finally:
        if server:
            if thread and thread.is_alive():server.shutdown()
            server.server_close()
        if thread:thread.join(2)
        close_native_sessions()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-live',action='store_true');parser.add_argument('--data',default='native-dashboard-proof');parser.add_argument('--env-file',type=Path);parser.add_argument('--prompt-file',type=Path);parser.add_argument('--chrome',default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');args=parser.parse_args()
    if not args.run_live:print('SKIP: genuine native dashboard execution requires --run-live. No Codex login or model turn requested.');return
    if args.env_file:load_env_file(args.env_file)
    run(args.data,args.chrome,args.prompt_file.read_text() if args.prompt_file else OBJECTIVE)

if __name__=='__main__':main()
