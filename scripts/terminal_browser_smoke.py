"""Real Chrome file execution and canonical Git. AI inference is not exercised."""
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from relay.app_server import AppServerClient,server_command  # noqa: E402
from relay.native_environment import root as native_root  # noqa: E402
from test_local_native_dashboard import LocalNativeDashboardTests  # noqa: E402
from test_shared_server import PASSWORD  # noqa: E402


def main():
    from playwright.sync_api import sync_playwright
    binary=shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))
    if not binary:raise RuntimeError('Installed Codex CLI is required for actual command proof')
    tools=native_root()/'tools'
    class RealConsoleClient(AppServerClient):
        def __init__(self,command,env,cwd):super().__init__(server_command(binary),env,cwd)
    case=LocalNativeDashboardTests();case.setUp()
    transport=patch('relay.console.AppServerClient',RealConsoleClient);transport.start()
    output=ROOT/'ide-proof';output.mkdir(exist_ok=True)
    report={'verified':False,'execution':'actual Codex command sandbox; no model turn','git':'actual disposable canonical repository','checks':[]}
    try:
        if tools.exists():shutil.copytree(tools,native_root()/'tools',symlinks=True)
        with case.server.relay.lock:
            for index in range(600):case.server.relay.event({'id':None,'conversation_id':case.conversation},'prompt','Historical prompt '+str(index),'user')
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            context=browser.new_context(viewport={'width':1680,'height':1050},permissions=['clipboard-read','clipboard-write'])
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(case.server.public_url);page.locator('#accountEmail').fill('owner@relay.invalid');page.locator('#accountPassword').fill(PASSWORD);page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function('() => !!window.RelayIDE?.editor && !!window.RelayIDE.getState()')
            page.wait_for_function('() => document.querySelectorAll("#timeline .activity-item").length>0')
            assert page.evaluate('events.length')==100
            page.locator('#loadOlderActivity').click();page.wait_for_function('() => events.length===200')
            assert page.locator('#timeline .activity-item').count()<=180
            report['checks'].append('Recent startup history, older cursor pagination and bounded activity DOM')

            def create(path,text):
                page.locator('[data-pane=explorer]').click()
                page.once('dialog',lambda dialog:dialog.accept(path));page.locator('#newFile').click()
                page.wait_for_function('(path)=>window.RelayIDE.activeFile===path',arg=path)
                page.evaluate('(text)=>window.RelayIDE.editor.setValue(text)',text)
            def wait_exit(code,marker):
                page.wait_for_function('([code,marker])=>document.getElementById("consoleState").textContent.includes("exit "+code) && document.getElementById("consoleOutput").textContent.includes(marker)',arg=[code,marker],timeout=45000)
            def command(text):
                page.locator('#consoleOutput').click(position={'x':40,'y':20})
                page.wait_for_function('() => document.activeElement.id==="commandInput" && !document.getElementById("commandInput").readOnly')
                # Individual key events, as a person types; no programmatic value set.
                page.keyboard.type(text);page.keyboard.press('Enter')

            create('hello.py','print("hello from Relay")\n')
            page.locator('#runFile').click();wait_exit(0,'hello from Relay')
            report['checks'].append('Run File saves and executes actual hello.py; stdout, exit code and duration')
            page.evaluate('(text)=>window.RelayIDE.editor.setValue(text)','name = input("Name? ")\nprint("hello " + name)\n')
            report['before_f5']=page.evaluate('() => ({file:window.RelayIDE.activeFile,disabled:document.getElementById("runFile").disabled,state:document.getElementById("consoleState").textContent,lock:window.RelayIDE.getState().lock?.kind})')
            page.wait_for_function('() => !document.getElementById("runFile").disabled')
            page.evaluate('() => window.RelayIDE.editor.focus()');page.keyboard.press('F5')
            page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("Name? ") && !document.getElementById("stdinForm").hidden',timeout=45000)
            page.reload();page.wait_for_function('() => !document.getElementById("stdinForm").hidden',timeout=30000)
            page.wait_for_selector('#stdinInput',state='visible',timeout=10000)
            page.locator('#stdinInput').fill('Ada');page.locator('#stdinForm button:not([type]), #stdinForm button[type=submit]').first.click();wait_exit(0,'hello Ada')
            report['checks'].append('F5, actual stdin and same-session browser refresh/reconnection')

            create('slow.py','import time\nprint("cancel-ready")\ntime.sleep(60)\n')
            page.locator('#runFile').click();page.wait_for_function('() => document.getElementById("consoleState").textContent==="running" && document.getElementById("consoleOutput").textContent.includes("cancel-ready")',timeout=45000)
            page.locator('#stopFile').click();page.wait_for_function('() => document.getElementById("consoleState").textContent.includes("finished")',timeout=10000)
            page.locator('#clearTerminal').click()
            page.locator('#runFile').click();page.wait_for_function('() => document.getElementById("consoleState").textContent==="running" && document.getElementById("consoleOutput").textContent.includes("cancel-ready")',timeout=45000)
            page.locator('#consoleOutput').focus();page.keyboard.press('Control+c');page.wait_for_function('() => document.getElementById("consoleState").textContent.includes("finished")',timeout=10000)
            report['checks'].append('Unbuffered live output without explicit flush; Run/Stop and terminal Ctrl+C actually stop the sandboxed process')

            create('fail.py','import sys\nprint("failure-stderr", file=sys.stderr)\nraise SystemExit(7)\n')
            page.locator('#runFile').click();wait_exit(7,'failure-stderr')
            create('hello.js','console.log("node hello");\n')
            page.locator('#fileTree [data-open-file="hello.js"]').click(button='right');page.locator('[data-file-action=run]').click();wait_exit(0,'node hello')
            report['checks'].append('Actual stderr/nonzero exit and approved Node Run Current File context action')
            create('package.json',json.dumps({'name':'terminal-proof','version':'1.0.0','scripts':{'build':'node hello.js'}}))
            command('npm run build');wait_exit(0,'terminal-proof@1.0.0 build')
            command('python3 hello.py');page.wait_for_function('() => document.getElementById("consoleState").textContent==="running" && !document.getElementById("stdinForm").hidden',timeout=45000)
            page.locator('#consoleOutput').click(position={'x':40,'y':20});page.keyboard.type('Lin');page.keyboard.press('Enter');wait_exit(0,'hello Lin')
            report['checks'].append('Actual typed npm run build and python3 hello.py with stdin, without network/package installation')

            # Focus survives a panel switch and drag resize; use actual keys.
            page.locator('#debugTab').click();page.locator('#consoleTab').click()
            assert page.evaluate('document.activeElement.id')=='commandInput'
            handle=page.locator('#bottomResize').bounding_box()
            page.mouse.move(handle['x']+50,handle['y']+2);page.mouse.down();page.mouse.move(handle['x']+50,handle['y']-35);page.mouse.up()
            assert page.evaluate('document.activeElement.id')=='commandInput'
            page.keyboard.type('git statuz');page.keyboard.press('Backspace');page.keyboard.type('s');page.keyboard.press('Home');page.keyboard.press('End');page.keyboard.press('Enter');wait_exit(0,'hello.py')
            command('git status');wait_exit(0,'hello.py')
            command('git add .');wait_exit(0,'authoritative project Git')
            command('git restore --staged hello.py');wait_exit(0,'git restore --staged hello.py')
            command('git add hello.py');wait_exit(0,'git add hello.py')
            command('git commit -m "slight improvements"');wait_exit(0,'slight improvements')
            workspace=case.server.relay.ws.path(case.project)
            commit=case.server.relay.ws.git(workspace,'rev-parse','HEAD')
            assert case.server.relay.ws.git(workspace,'log','-1','--format=%s')=='slight improvements'
            command('git log --oneline');wait_exit(0,'slight improvements')
            command('git branch terminal-check');wait_exit(0,'terminal-check')
            command('git switch terminal-check');wait_exit(0,'terminal-check')
            page.locator('#commandInput').press('ArrowUp');assert page.locator('#commandInput').input_value()=='git switch terminal-check'
            page.evaluate('navigator.clipboard.writeText("git status")');page.locator('#consoleOutput').click(position={'x':40,'y':20});page.keyboard.press('Control+c');page.wait_for_function('() => document.getElementById("commandInput").value===""');page.keyboard.press('Meta+v');page.wait_for_function('() => document.getElementById("commandInput").value==="git status"');page.keyboard.press('Enter');wait_exit(0,'authoritative project Git')
            command('python3 -c "print(\'quoted args\')"');wait_exit(0,'quoted args')
            report.update({'commit':commit,'commit_subject':'slight improvements'})
            report['checks'].append('Typed status/add/restore/quoted commit/log/branch/switch, actual Git commit, quoted Python argv and command history')
            command('git push');page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("Remote Git pushes are unavailable")')
            page.wait_for_function('() => document.getElementById("consoleState").textContent==="Command rejected"')
            page.locator('#copyTerminal').click();page.wait_for_function('() => document.getElementById("notice").textContent.includes("copied")')
            assert 'Remote Git pushes are unavailable' in page.evaluate('navigator.clipboard.readText()')
            page.screenshot(path=str(output/'terminal-desktop.png'),full_page=True)
            page.locator('#clearTerminal').click();assert page.locator('#consoleOutput').inner_text()==''
            report['checks'].append('Remote push rejected explicitly, clipboard copy and clear display without deleting events')
            assert case.server.relay.db.execute("SELECT COUNT(*) FROM events WHERE event_type='prompt' AND content LIKE 'Historical prompt %'").fetchone()[0]==600
            report['command_results']=[{'output':row['content'],'metadata':json.loads(row['metadata'])} for row in case.server.relay.rows("SELECT content,metadata FROM events WHERE project_id=? AND event_type='tool_output' AND json_extract(metadata,'$.console_id') IS NOT NULL",(case.project,))]
            report['browser_errors']=errors
            if errors:raise AssertionError(errors)
            report['verified']=True;context.close();browser.close()
    finally:
        report['command_results']=[{'output':row['content'],'metadata':json.loads(row['metadata'])} for row in case.server.relay.rows("SELECT content,metadata FROM events WHERE project_id=? AND event_type='tool_output' AND json_extract(metadata,'$.console_id') IS NOT NULL",(case.project,))]
        report['commands']=[row['content'] for row in case.server.relay.rows("SELECT content FROM events WHERE project_id=? AND event_type='tool_invocation' AND json_extract(metadata,'$.console_id') IS NOT NULL",(case.project,))]
        (output/'terminal-browser-report.json').write_text(json.dumps(report,indent=2))
        transport.stop();case.tearDown()
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
