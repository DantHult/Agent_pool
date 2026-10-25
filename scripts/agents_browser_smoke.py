"""Real Chrome Agents sidebar and per-agent Stop. Uses the mock native RPC
fixture: process control is genuine, AI inference is not exercised."""
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from relay.native import close_native_sessions,profile_paths,session_for  # noqa: E402
from test_local_native_dashboard import LocalNativeDashboardTests  # noqa: E402
from test_native import PROMPT  # noqa: E402
from test_shared_server import PASSWORD  # noqa: E402

CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def alive(pid):
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False


def main():
    from playwright.sync_api import sync_playwright
    case=LocalNativeDashboardTests();case.setUp()
    output=ROOT/'ide-proof';output.mkdir(exist_ok=True)
    report={'verified':False,'execution':'mock native App Server fixture; real process groups; no model inference','checks':[]}
    try:
        close_native_sessions()
        for profile in ('a','b'):
            home,_=profile_paths(profile);(home/'scenario.json').write_text('{"continuous":2,"delay":90,"child":true}')
        a,b=case.enroll('a'),case.enroll('b')
        other=case.client.post('projects',{'name':'Second running project'})['id']
        case.client.post('workers/configure',{'worker_id':b,'project_ids':[case.project,other]})
        task_a=case.client.post('tasks/assign',{'worker_id':a,'project_id':case.project,'prompt':PROMPT})
        task_b=case.client.post('tasks/assign',{'worker_id':b,'project_id':other,'prompt':PROMPT})
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not all((profile_paths(p)[0]/'child.pid').exists() for p in ('a','b')):time.sleep(.05)
        children={p:int((profile_paths(p)[0]/'child.pid').read_text()) for p in ('a','b')}
        servers={p:session_for(p).client.process for p in ('a','b')}
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=CHROME,headless=True)
            context=browser.new_context(viewport={'width':1680,'height':1050})
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(case.server.public_url);page.locator('#accountEmail').fill('owner@relay.invalid');page.locator('#accountPassword').fill(PASSWORD);page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function('() => !!window.RelayIDE?.getState()')
            page.locator('[data-pane=agents]').click()
            card=lambda worker:page.locator(f'[data-agent-observation="{worker}"]')
            stop=lambda worker:page.locator(f'[data-stop-agent="{worker}"]')
            stop(a).wait_for(state='visible',timeout=20000)
            summary=card(a).locator('.agent-live-summary strong').inner_text()
            assert summary.lower().startswith('running'),summary
            # The fixture's last recorded tool event; activity must come from it.
            assert 'Command finished (exit 0): read existing project' in summary or 'Running command:' in summary,summary
            elapsed=card(a).locator('[data-agent-elapsed]').inner_text();time.sleep(1.5)
            assert card(a).locator('[data-agent-elapsed]').inner_text()!=elapsed,'elapsed time is not live'
            card(a).locator('.agent-details > summary').click()
            assert 'Last command' in card(a).locator('.agent-details').inner_text()
            report['checks'].append('Live card: running state, current activity, ticking elapsed time and expandable details')
            report['summary_a']=summary

            stop(a).click()
            page.wait_for_function('(worker)=>{const n=document.querySelector(`[data-agent-observation="${worker}"] .agent-live-summary strong`);return n && /^(stopped|stopping)/i.test(n.textContent)}',arg=a,timeout=20000)
            case.wait(task_a,'cancelled')
            page.wait_for_function('(worker)=>/^stopped/i.test(document.querySelector(`[data-agent-observation="${worker}"] .agent-live-summary strong`)?.textContent||"")',arg=a,timeout=20000)
            assert stop(a).count()==0
            assert servers['a'].poll() is not None,'agent A App Server still running'
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and alive(children['a']):time.sleep(.05)
            assert not alive(children['a']),'agent A tool child survived Stop'
            assert servers['b'].poll() is None and alive(children['b']),'Stop affected agent B'
            assert stop(b).is_visible() and case.client.get('tasks?project_id='+other)['tasks'][0]['state']=='running'
            assert not case.server.relay.rows('SELECT * FROM workspace_leases WHERE project_id=?',(case.project,))
            assert 'native_tools.py' in case.server.relay.ws.inspect(case.project)['files']
            report['checks'].append('Stop A in Chrome: A App Server and tool child terminated, lease released, partial files kept; B untouched')

            stop(b).click();case.wait(task_b,'cancelled')
            page.wait_for_function('(worker)=>!document.querySelector(`[data-stop-agent="${worker}"]`)',arg=b,timeout=20000)
            report['checks'].append('Stop B separately')
            page.screenshot(path=str(output/'agents-sidebar.png'),full_page=True)
            report['browser_errors']=errors
            if errors:raise AssertionError(errors)
            report['verified']=True;context.close();browser.close()
    finally:
        (output/'agents-browser-report.json').write_text(json.dumps(report,indent=2))
        case.tearDown()
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
