"""Real isolated Chrome sessions on one host; explicitly SIMULATED inference.

Optional dev dependency: Playwright. Uses installed Chrome, not personal profiles.
Never loads native Codex credentials, calls a provider, or exposes a public port.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.server import make_server  # noqa: E402

PROMPT='Add a password-strength validator and tests to this sample project.'


def run(data,chrome):
    from playwright.sync_api import sync_playwright
    directory=Path(data)
    if (directory/'relay.sqlite').exists():raise ValueError('Use a new dedicated proof directory; prior proof is never overwritten')
    server=make_server(directory,0,'invite','http://127.0.0.1:0',True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    password=secrets.token_urlsafe(24);origin=server.public_url
    try:
        admin_id=server.identity_provider.bootstrap('organizer@relay.invalid','Organizer',password)['id']
        project=server.relay.mutate(admin_id,'projects',{'name':'Three-browser handoff proof'})['id']
        workers=[server.relay.mutate(admin_id,'workers/enroll',{'display_name':'Simulated Worker '+label,'owner_type':'org','execution_mode':'simulation','project_ids':[project]})['id'] for label in ('A','B','C')]
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=chrome,headless=True)
            contexts=[]
            def page():
                context=browser.new_context(viewport={'width':1450,'height':1050});contexts.append(context)
                return context.new_page()
            def signin(tab,email):
                tab.goto(origin);tab.locator('#accountEmail').fill(email);tab.locator('#accountPassword').fill(password)
                tab.locator('#accountForm button[type=submit]').click();tab.wait_for_function("() => !document.body.classList.contains('signed-out')")
            admin=page();signin(admin,'organizer@relay.invalid')
            members=[]
            for name in ('Alex','Sam','Jordan'):
                admin.locator('#inviteMember').click();admin.locator('#inviteEmail').fill(name.lower()+'@relay.invalid')
                admin.locator('#inviteForm button[type=submit]').click()
                admin.wait_for_function("() => document.getElementById('inviteLink').value.includes('#invite=')")
                invitation=admin.locator('#inviteLink').input_value();admin.locator('#closeInvite').click()
                tab=page();tab.goto(invitation)
                tab.locator('#accountEmail').fill(name.lower()+'@relay.invalid');tab.locator('#accountName').fill(name)
                tab.locator('#accountPassword').fill(password);tab.locator('#accountForm button[type=submit]').click()
                tab.wait_for_function("() => !document.body.classList.contains('signed-out')")
                members.append(tab)
            alex,sam,jordan=members
            alex.locator('#startWorker').select_option(workers[0]);alex.locator('#prompt').fill(PROMPT)
            alex.locator('#pauseAfterTurn').check();alex.locator('#handoffPhases').select_option('3');alex.locator('.send').click()
            sam.wait_for_function("() => document.getElementById('timeline').textContent.includes('validator.py written')")
            conversation=alex.locator('#conversationId').inner_text()
            sam.locator('#pause').click();sam.wait_for_function("() => document.getElementById('taskState').textContent==='handoff_pending'")
            sam.locator('#switchTarget').select_option(workers[1]);sam.locator('#switch').click()
            jordan.wait_for_function("() => document.getElementById('timeline').textContent.includes('SIMULATED phase B wrote')")
            jordan.locator('#pause').click();jordan.wait_for_function("() => document.getElementById('taskState').textContent==='handoff_pending'")
            jordan.locator('#switchTarget').select_option(workers[2]);jordan.locator('#switch').click()
            jordan.wait_for_function("() => document.getElementById('taskState').textContent==='completed'")
            for tab in members:
                tab.reload();tab.wait_for_function("() => document.getElementById('taskState').textContent==='completed'")
                assert tab.locator('#conversationId').inner_text()==conversation
                assert 'Ran 5 tests' in tab.locator('#timeline').inner_text()
            with server.relay.lock:
                detail=server.relay.detail(admin_id,project);task=detail['tasks'][0]
                assert len(detail['execution_runs'])==3 and not server.relay.rows('SELECT * FROM workspace_leases')
                server.relay.event(task,'tool_output','AUTHORIZED-PROJECT-ARTIFACT '*1000)
                artifact=server.relay.rows("SELECT json_extract(metadata,'$.artifact_id') AS id FROM events WHERE json_extract(metadata,'$.artifact_id') IS NOT NULL LIMIT 1")[0]['id']
            # Fourth account is invited to the organization, without project access.
            invited=server.identity_provider.invite(admin_id,{'email':'outsider@relay.invalid'})
            outsider=page();outsider.goto(origin+'/#invite='+invited['invitation_token'])
            outsider.locator('#accountEmail').fill('outsider@relay.invalid');outsider.locator('#accountName').fill('Outsider')
            outsider.locator('#accountPassword').fill(password);outsider.locator('#accountForm button[type=submit]').click()
            outsider.wait_for_function("() => !document.body.classList.contains('signed-out')")
            for route in (f'projects/{project}',f'projects/{project}/download?file=validator.py',f'conversations/{conversation}/events',f'conversations/{conversation}/stream',f'artifacts/{artifact}',f'tasks/{task["id"]}/result'):
                status=outsider.evaluate("async path => (await fetch('/api/'+path)).status",route)
                assert status==403,(route,status)
            port=server.server_port
            server.shutdown();server.server_close();thread.join(2)
            server=make_server(directory,port,'invite',origin,True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            for tab in members:
                tab.reload();tab.wait_for_function("() => document.getElementById('taskState').textContent==='completed'")
                assert tab.locator('#conversationId').inner_text()==conversation
                assert 'Ran 5 tests' in tab.locator('#timeline').inner_text()
            with server.relay.lock:restored=server.relay.detail(admin_id,project)
            assert restored['execution_runs']==detail['execution_runs'] and restored['checkpoints']==detail['checkpoints']
            alex.locator('[data-tab=checkpoints]').click();alex.screenshot(path=str(directory/'three-browser-proof.png'),full_page=True)
            report={'mode':'simulated-inference','browser':'real installed Chrome','isolated_browser_contexts':len(contexts),'authorized_humans':3,'unauthorized_account_blocked':True,'restart_persistence':True,'conversation_id':conversation,'task_id':task['id'],'project_id':project,'worker_ids':workers,'execution_runs':restored['execution_runs'],'checkpoint_ids':[c['git_commit'] for c in restored['checkpoints']],'generated_tests_passed':5,'real_multi_host_verified':False,'live_organization_api_verified':False}
            (directory/'multi-browser-report.json').write_text(json.dumps(report,indent=2))
            for context in contexts:context.close()
            browser.close()
        print('PASS: three real Chrome member sessions, independent Organizer/Outsider sessions, two worker handoffs, five real fixture tests, reload/restart persistence and unauthorized reads blocked. Inference was SIMULATED; physical multi-host/live organization execution NOT VERIFIED.')
        print('Proof:',str((directory/'multi-browser-report.json').resolve()))
    finally:server.shutdown();server.server_close();thread.join(2)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',default='multi-proof');parser.add_argument('--chrome',default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');args=parser.parse_args()
    run(args.data,args.chrome)

if __name__=='__main__':main()
