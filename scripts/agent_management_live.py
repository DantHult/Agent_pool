"""Opt-in actual native task through Add Agent / Assign Task in Chrome.

Uses an existing authenticated profile unchanged. Never stops another Relay,
reads credentials, or falls back to simulated inference. Use a fresh proof path.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from relay.accounts import InviteIdentityProvider  # noqa: E402
from relay.native import native_readiness,close_native_sessions  # noqa: E402
from relay.server import make_server  # noqa: E402
from relay.store import Relay  # noqa: E402

OBJECTIVE=('Create clamp.py with clamp(value, lower, upper). Return the value bounded by the inclusive limits; '
           'raise ValueError for inverted limits. Add test_clamp.py with at least four Python unittest cases '
           'covering boundaries and invalid limits. Run python3 -m unittest -v test_clamp. Finish the task in this turn. '
           'No network or extra packages are needed. Report the actual test result.')


def run(directory, profile, chrome):
    from playwright.sync_api import sync_playwright
    if directory.exists():raise ValueError('Use a fresh proof directory to preserve previous evidence')
    directory.mkdir(parents=True,mode=0o700)
    report={'live_verified':False,'profile':profile,'inference':'actual native required; no mock fallback','stage':'preflight'}
    report_path=directory/'direct-agent-report.json'
    def save():report_path.write_text(json.dumps(report,indent=2))
    save();server=None;thread=None
    try:
        checks,details=native_readiness((profile,))
        report['preflight']=[{'check':label,'passed':passed} for label,passed in checks]
        if not all(passed for _,passed in checks):
            report.update(stage='blocked',blockers=details);save()
            print('BLOCKED: '+'; '.join(details));return False
        password=secrets.token_urlsafe(24);email='direct-agent-proof@relay.invalid'
        relay=Relay(directory/'data',demo=False)
        owner=InviteIdentityProvider(relay).bootstrap(email,'Direct Agent Proof',password,local_owner=True)['id'];relay.close()
        server=make_server(directory/'data',0,'local-owner');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with server.relay.lock:project=server.relay.create_project(owner,'Disposable direct native task')['id']
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=chrome,headless=True)
            context=browser.new_context(viewport={'width':1680,'height':1050});page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server.public_url);page.locator('#accountEmail').fill(email);page.locator('#accountPassword').fill(password)
            page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function('() => !!window.RelayIDE?.getState()')
            page.locator('[data-pane=agents]').click();page.locator('#enroll').click()
            page.locator('#workerName').fill('Direct Native '+profile.upper());page.locator('#nativeProfile').select_option(profile)
            page.locator('#enrollmentForm button[type=submit]').click()
            page.wait_for_selector('[data-assign-worker]')
            with server.relay.lock:worker=server.relay.workers(owner,project)['workers'][0]['id']
            page.locator('[data-assign-worker="'+worker+'"]').click();page.locator('#assignPrompt').fill(OBJECTIVE)
            page.locator('#assignQueue').uncheck();report.update(stage='actual-native-turn',worker_id=worker,project_id=project);save()
            page.locator('#submitAssignment').click()
            page.wait_for_function('() => document.getElementById("taskState").textContent==="completed"',timeout=240000)
            with server.relay.lock:
                tasks=server.relay.management.tasks(owner,project)['tasks'];task=tasks[0]
                runs=server.relay.execution_result(owner,task['id'])['runs']
                if task['worker_id']!=worker or task['requested_worker_id']!=worker:raise ValueError('Task did not run on its selected agent')
                if len(runs)!=1 or runs[0]['execution_mode']!='native' or runs[0]['state']!='completed' or runs[0]['exit_code']!=0:raise ValueError('Actual completed native execution was not verified')
                events=server.relay.rows("SELECT event_type,content,metadata FROM events WHERE task_id=? ORDER BY sequence",(task['id'],))
                tests=[event for event in events if event['event_type']=='tool_output' and json.loads(event['metadata']).get('exit_code')==0 and 'Ran ' in event['content'] and 'OK' in event['content']]
                if not tests:raise ValueError('No successful actual unittest output was observed')
                review=server.relay.management.review(owner,project,task['id'])
                if not {'clamp.py','test_clamp.py'}.issubset(review['files']):raise ValueError('Expected coding changes were not checkpointed')
            page.locator('[data-inspect-task="'+task['id']+'"]').click()
            page.wait_for_function('() => window.RelayIDE.activeFile==="Agent checkpoint changes.diff"')
            page.reload();page.wait_for_function('() => document.getElementById("taskState").textContent==="completed"')
            page.screenshot(path=str(directory/'direct-agent.png'),full_page=True)
            if errors:raise ValueError('Browser reported errors')
            report.update(live_verified=True,stage='verified',task_id=task['id'],runs=runs,review=review,test_output=tests[-1]['content'],browser_errors=errors)
            save();context.close();browser.close()
        print('PASS: actual selected-profile native task, tests, checkpoint diff and browser refresh. Proof: '+str(report_path))
        return True
    except Exception as exc:
        # Browser exception strings can contain form inputs. Retain only type.
        report.update(stage='failed',failure_type=type(exc).__name__);save()
        print('FAILED: '+type(exc).__name__+'; inspect the private proof and saved task events.')
        return False
    finally:
        if server:
            server.shutdown();server.server_close()
        if thread:thread.join(2)
        close_native_sessions()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true');parser.add_argument('--profile',choices=('a','b','c'),default='a')
    parser.add_argument('--data',type=Path,default=Path('ide-proof/direct-native'))
    parser.add_argument('--chrome',default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    args=parser.parse_args()
    if not args.run_live:print('SKIP: --run-live is required; no provider request made.');return
    if not run(args.data,args.profile,args.chrome):raise SystemExit(2)


if __name__=='__main__':main()
