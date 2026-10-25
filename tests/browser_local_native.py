"""Optional real-Chrome local-owner UI regression; mocked native inference only.

Run with optional Playwright on PYTHONPATH. Uses private temporary accounts and
profiles, never a personal browser profile or real Codex authentication.
"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_local_native_dashboard import LocalNativeDashboardTests,OBJECTIVE  # noqa: E402
from test_shared_server import PASSWORD  # noqa: E402
from relay.native import profile_paths  # noqa: E402


def main():
    from playwright.sync_api import sync_playwright
    case=LocalNativeDashboardTests();case.setUp()
    try:
        home,_=profile_paths('a');(home/'scenario.json').write_text('{"delay":6}')
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            context=browser.new_context();page=context.new_page();page.goto(case.server.public_url)
            page.locator('#accountEmail').fill('owner@relay.invalid');page.locator('#accountPassword').fill(PASSWORD)
            page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function("() => document.querySelectorAll('[data-native-profile]').length===3")
            page.locator('[data-pane=agents]').click()
            # Selecting immediately after initial profiles appear used to race
            # the first project load and discard the selected worker.
            page.locator('[data-native-profile=a]').click()
            page.wait_for_function("() => document.getElementById('startWorker').value!==''")
            page.locator('#prompt').fill(OBJECTIVE);page.locator('.run-options summary').click();page.locator('#pauseAfterTurn').check();page.locator('.run-options summary').click();page.locator('.send').click()
            page.wait_for_function("() => document.getElementById('taskState').textContent==='running'")
            assert 'NATIVE LOCAL' in page.locator('#connectionTitle').inner_text()
            assert 'simulated' not in page.locator('#connectionTitle').inner_text().lower()
            page.evaluate('() => window.RelayIDE.openFile("README.md")')
            page.wait_for_function('() => window.RelayIDE.activeFile==="README.md"')
            assert page.evaluate('window.RelayIDE.editor.getOption(window.RelayMonaco.editor.EditorOption.readOnly)')
            assert 'agent is writing' in page.locator('#workspaceLock').inner_text().lower()
            page.locator('#viewWriter').click()
            page.locator('#pause').click()
            page.wait_for_function("() => document.getElementById('taskState').textContent==='handoff_pending'")
            page.wait_for_function('() => window.RelayIDE.getState().lock?.mine && !window.RelayIDE.editor.getOption(window.RelayMonaco.editor.EditorOption.readOnly)')
            assert 'NATIVE LOCAL' in page.locator('#timeline').inner_text()
            context.close();browser.close()
        print('PASS: real Chrome profile-selection timing, busy NATIVE LOCAL label and owner Pause/Checkpoint. Mocked inference; no model call.')
    finally:case.tearDown()

if __name__=='__main__':main()
