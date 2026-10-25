"""Real Chrome IDE regression: mocked AI only, genuine isolated console commands.

Temporary owner account/profiles only. Never uses a personal browser, real Codex
authentication, or a model turn. Output reports distinguish those boundaries.
"""
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
from test_local_native_dashboard import LocalNativeDashboardTests,OBJECTIVE  # noqa: E402
from test_shared_server import PASSWORD  # noqa: E402


def main():
    from playwright.sync_api import sync_playwright
    binary=shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))
    if not binary:raise RuntimeError('Actual installed Codex CLI required for console proof')
    approved_tools=native_root()/'tools'
    class RealConsoleClient(AppServerClient):
        def __init__(self,command,env,cwd):super().__init__(server_command(binary),env,cwd)
    case=LocalNativeDashboardTests();case.setUp()
    transport=patch('relay.console.AppServerClient',RealConsoleClient);transport.start()
    report={'ai_inference':'mocked protocol fixture','console_execution':'actual Codex command sandbox; unauthenticated home; no model request','verified':False,'checks':[]}
    output=ROOT/'ide-proof';output.mkdir(exist_ok=True)
    try:
        if approved_tools.exists():shutil.copytree(approved_tools,native_root()/'tools',symlinks=True)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            context=browser.new_context(viewport={'width':1536,'height':960});page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(case.server.public_url)
            page.locator('#accountEmail').fill('owner@relay.invalid');page.locator('#accountPassword').fill(PASSWORD);page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function('() => !!window.RelayIDE?.getState()')
            page.wait_for_function('() => !!window.RelayIDE?.editor',timeout=30000)
            page.locator('[data-open-file="README.md"]').first.click()
            page.wait_for_function('() => window.RelayIDE.activeFile === "README.md"')
            page.wait_for_function('() => window.RelayIDE.getState().lock?.mine && !window.RelayIDE.editor.getOption(window.RelayMonaco.editor.EditorOption.readOnly)')
            assert page.locator('#editLock,#releaseLock,#enableConsole').count()==0
            report['checks'].append('Editor editable immediately; no editing-mode or console enablement controls')
            page.evaluate('() => window.RelayIDE.editor.setValue("# Browser edited project\\nSaved through Monaco.\\n")')
            page.locator('#saveFile').click();page.wait_for_function('() => document.getElementById("saveState").textContent === "Saved"')
            assert 'Browser edited project' in case.client.get('ide/file?project_id='+case.project+'&path=README.md')['content']
            report['checks'].append('Monaco open/edit/atomic save')
            page.evaluate('() => window.RelayIDE.editor.setValue("# Retained browser draft\\nNot yet in the working file.\\n")')
            page.wait_for_function('() => document.getElementById("saveState").textContent === "Draft retained"')
            page.reload();page.wait_for_function('() => window.RelayIDE.editor?.getValue().includes("Retained browser draft")')
            page.locator('#saveFile').click();page.wait_for_function('() => document.getElementById("saveState").textContent === "Saved"')
            report['checks'].append('Draft + edit lock restored after refresh')
            page.locator('[data-pane=source]').click();page.locator('#stageAll').click()
            page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("Exit 0")')
            page.locator('#commitMessage').fill('Browser IDE source commit');page.locator('#commitChanges').click()
            page.wait_for_function('() => document.getElementById("commitMessage").value === ""')
            report['checks'].append('Real Git stage/commit and checkpoint')
            page.wait_for_function('() => !document.getElementById("commandInput").disabled',timeout=30000)
            page.locator('#commandInput').fill('python3 -u -c \'print("console-ready",flush=True); print("echo:"+input())\'');page.locator('#runCommand').click()
            page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("\\nconsole-ready\\n")')
            page.locator('#stdinInput').fill('browser-input');page.locator('#stdinForm button[type=submit], #stdinForm button:not([type])').first.click()
            page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("echo:browser-input")')
            page.wait_for_function('() => document.getElementById("consoleState").textContent.includes("exit 0")')
            report['checks'].append('Actual isolated command streaming + browser stdin')
            if approved_tools.exists():
                for command,expected in [('node --version','v24.'),('npm --version','12.')]:
                    page.locator('#commandInput').fill(command);page.locator('#runCommand').click()
                    page.wait_for_function('(expected)=>document.getElementById("consoleState").textContent.includes("exit 0") && document.getElementById("consoleOutput").textContent.includes("\\n"+expected)',arg=expected)
                report['checks'].append('Actual approved Node/npm execution inside the command sandbox')
            page.evaluate('() => window.RelayIDE.editor.setValue("# Saved automatically for the next command\\n")')
            page.locator('#commandInput').fill('python3 -c \'from pathlib import Path; print(Path("README.md").read_text())\'');page.locator('#runCommand').click()
            page.wait_for_function('() => document.getElementById("consoleState").textContent.includes("exit 0") && document.getElementById("consoleOutput").textContent.includes("Saved automatically for the next command")')
            report['checks'].append('Run saves editor drafts and refreshes an unchanged stale command snapshot')
            page.locator('#commandInput').fill('python3 -c \'from pathlib import Path; Path("browser_console.py").write_text("console_value = 42\\n")\'');page.locator('#runCommand').click()
            page.wait_for_function('() => document.getElementById("consoleState").textContent.includes("exit 0")')
            page.locator('#applyConsole').click();page.wait_for_function('() => document.getElementById("notice").textContent.includes("Console changes applied")')
            assert 'console_value = 42' in case.client.get('ide/file?project_id='+case.project+'&path=browser_console.py')['content']
            report['checks'].append('Explicit console publish + consistent Git checkpoint')
            page.evaluate('() => window.RelayIDE.editor.setValue("# Human draft saved automatically before the agent\\n")')
            page.wait_for_function('() => document.getElementById("saveState").textContent === "Draft retained"')
            page.locator('[data-pane=agents]').click();page.locator('[data-native-profile=a]').click()
            page.wait_for_function('() => document.getElementById("startWorker").value !== ""')
            page.locator('#prompt').fill(OBJECTIVE)
            page.locator('.run-options summary').click();page.locator('#pauseAfterTurn').check();page.locator('.run-options summary').click();page.locator('.send').click()
            page.wait_for_function('() => document.getElementById("taskState").textContent === "handoff_pending"',timeout=30000)
            assert 'NATIVE LOCAL' in page.locator('#timeline').inner_text()
            page.locator('[data-native-profile=b]').click();page.wait_for_function('() => document.getElementById("switchTarget").value !== ""');page.locator('#switch').click()
            page.wait_for_function('() => document.getElementById("taskState").textContent === "completed"',timeout=30000)
            assert 'Human draft saved automatically' in case.client.get('ide/file?project_id='+case.project+'&path=README.md')['content']
            assert page.locator('#timeline .operation-card').count()>0
            report['checks'].append('Automatic draft save/checkpoint/release, mocked AI stream, operations and explicit A-to-B handoff')
            # A large stream should remain one message, and final output should
            # replace rather than duplicate those fragments.
            task=case.server.relay.rows('SELECT * FROM tasks ORDER BY rowid DESC')[0]
            with case.server.relay.lock:
                for char in 'Streaming projection regression '*25:
                    case.server.relay.event(task,'agent_delta',char,'assistant',{'origin':'codex','provider_item_id':'ui-regression-message'})
                case.server.relay.event(task,'response','{"summary":"Readable final response after many fragments"}','assistant',{'origin':'codex','provider_item_id':'ui-regression-message'})
            page.wait_for_function('() => document.getElementById("timeline").textContent.includes("Readable final response after many fragments")')
            assert page.locator('#timeline .assistant').filter(has_text='Readable final response after many fragments').count()==1
            assert page.locator('#timeline .activity-item').count()<100
            report['checks'].append('Hundreds of deltas coalesced into one final response')
            page.locator('#timeline .operation-card summary').first.click();page.wait_for_timeout(700)
            assert page.locator('#timeline .operation-card').first.evaluate('(node)=>node.open')
            page.locator('#debugTab').click();page.wait_for_function('() => document.getElementById("history").textContent.length > 0')
            report['checks'].append('Expandable tools + secondary paginated raw events')
            page.locator('[data-pane=conversations]').click();page.once('dialog',lambda dialog:dialog.accept('Design followups'));page.locator('#newConversation').click()
            page.wait_for_function('() => document.querySelectorAll("#conversationList .conversation").length === 2')
            page.locator('#conversationList .conversation').filter(has_text='Design followups').click()
            page.wait_for_function('() => document.querySelectorAll("#timeline .activity-item").length === 0')
            page.reload();page.wait_for_function('() => document.querySelector("#conversationList .selected")?.textContent.includes("Design followups")')
            report['checks'].append('Multiple conversations + selection persistence')
            # Select original history for a representative wide-screen screenshot.
            page.locator('[data-pane=conversations]').click();page.locator('#conversationList .conversation').filter(has_text='Disposable native sample').click()
            page.wait_for_function('() => document.querySelectorAll("#timeline .activity-item").length > 0')
            page.locator('[data-pane=explorer]').click();page.evaluate('() => window.RelayIDE.openFile("native_tools.py")')
            if page.locator('#closeBottom').is_visible():page.locator('#closeBottom').click()
            before=page.locator('#agentPanel').bounding_box()['width'];page.locator('#agentResize').focus();page.keyboard.press('ArrowLeft')
            assert page.locator('#agentPanel').bounding_box()['width']>before
            page.keyboard.press('Meta+p');page.locator('#quickOpenInput').fill('README');page.keyboard.press('Enter')
            page.wait_for_function('() => window.RelayIDE.activeFile==="README.md"')
            page.evaluate('() => window.RelayIDE.openFile("native_tools.py")');page.mouse.move(600,300)
            report['checks'].append('Keyboard file navigation and accessible panel resizing')
            page.screenshot(path=str(output/'workspace-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844});page.locator('#toggleAgent').click()
            page.locator('#showAgentSettings').click();assert page.locator('#pane-agents').is_visible()
            page.locator('[data-pane=explorer]').click();page.locator('[data-open-file="native_tools.py"]').first.click()
            assert not page.locator('#agentPanel').is_visible()
            page.locator('#toggleAgent').click();page.screenshot(path=str(output/'workspace-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            report['checks'].append('Desktop/mobile layout without horizontal overflow')
            report['browser_errors']=errors
            if errors:raise AssertionError('Browser errors: '+str(errors))
            report['verified']=True
            context.close();browser.close()
    finally:
        if 'errors' in locals():report['browser_errors']=errors
        try:
            if 'page' in locals() and not page.is_closed() and not report['verified']:
                report['last_error']=page.locator('#error').text_content()
                report['console_state']=page.locator('#consoleState').text_content()
                report['console_output']=page.locator('#consoleOutput').text_content()
                page.screenshot(path=str(output/'failure.png'),full_page=True)
        except Exception:
            pass  # Preserve the original browser failure after Playwright closes.
        (output/'browser-report.json').write_text(json.dumps(report,indent=2))
        transport.stop();case.tearDown()
    print('PASS:',json.dumps(report))


if __name__=='__main__':main()
