"""Chrome management walkthrough. Native AI uses an explicitly mocked protocol."""
import json
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_local_native_dashboard import LocalNativeDashboardTests,OBJECTIVE  # noqa: E402
from test_shared_server import PASSWORD  # noqa: E402
from relay.server import make_server  # noqa: E402


def main():
    from playwright.sync_api import sync_playwright
    case=LocalNativeDashboardTests();case.setUp()
    report={'verified':False,'inference':'mocked App Server protocol; no subscription execution',
            'filesystem_git_http':'actual temporary project operations','checks':[]}
    output=ROOT/'ide-proof';output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            context=browser.new_context(viewport={'width':1680,'height':1050});page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(case.server.public_url)
            page.locator('#accountEmail').fill('owner@relay.invalid');page.locator('#accountPassword').fill(PASSWORD)
            page.locator('#accountForm button[type=submit]').click()
            page.wait_for_function('() => !!window.RelayIDE?.editor && !!window.RelayIDE?.getState()')

            def prompt(button,answer):
                handled=[]
                def accept(dialog):dialog.accept(answer);handled.append(True)
                page.once('dialog',accept);page.locator(button).click()
                for _ in range(100):
                    if handled:return
                    page.wait_for_timeout(50)
                raise AssertionError('Expected prompt did not open: '+button)
            def confirm(button,accept=True):
                handled=[]
                def respond(dialog):
                    if accept:dialog.accept()
                    else:dialog.dismiss()
                    handled.append(True)
                page.once('dialog',respond);page.locator(button).click()
                for _ in range(100):
                    if handled:return
                    page.wait_for_timeout(50)
                raise AssertionError('Expected confirmation did not open: '+button)
            def wait_path(path,exists=True):
                page.wait_for_function('([path,exists]) => window.RelayIDE.getState().files.some(file=>file.path===path)===exists',arg=[path,exists])
            def enroll(profile,name):
                page.locator('[data-pane=agents]').click();page.locator('#enroll').click()
                page.locator('#workerName').fill(name);page.locator('#nativeProfile').select_option(profile)
                page.locator('#enrollmentForm button[type=submit]').click()
                page.wait_for_function('(name)=>document.getElementById("workers").textContent.includes(name)',arg=name)
                return next(w['id'] for w in case.client.get('workers?project_id='+case.project)['workers'] if w['native_profile']==profile)
            def assign(worker,queue):
                page.locator('[data-assign-worker="'+worker+'"]').click()
                page.locator('#assignPrompt').fill(OBJECTIVE);page.locator('#assignQueue').set_checked(queue)
                page.locator('#submitAssignment').click();page.wait_for_function('() => !document.getElementById("assignDialog").open')

            assert page.locator('#editLock,#releaseLock,#enableConsole').count()==0
            prompt('#newFolder','src');page.wait_for_function('() => window.RelayIDE.getState().folders.includes("src")')
            prompt('#newFolder','src/empty');page.wait_for_function('() => window.RelayIDE.getState().folders.includes("src/empty")')
            prompt('#newFile','src/example.py');wait_path('src/example.py')
            page.wait_for_function('() => window.RelayIDE.activeFile==="src/example.py"')
            page.evaluate('() => window.RelayIDE.editor.setValue("answer = 42\\n")')
            page.locator('#saveFile').click();page.wait_for_function('() => document.getElementById("saveState").textContent==="Saved"')
            prompt('#saveAsFile','src/copy.py');wait_path('src/copy.py')
            page.wait_for_function('() => window.RelayIDE.activeFile==="src/copy.py"')
            prompt('#renameEntry','src/renamed.py');wait_path('src/renamed.py');wait_path('src/copy.py',False)
            page.wait_for_function('() => window.RelayIDE.activeFile==="src/renamed.py"')
            confirm('#deleteEntry',False);assert page.locator('#fileTree [data-open-file="src/renamed.py"]').count()==1
            confirm('#deleteEntry');wait_path('src/renamed.py',False)
            folder=page.locator('[data-folder="src/empty"] > summary');folder.click(button='right')
            prompt('[data-file-action=rename]','src/renamed-folder')
            page.wait_for_function('() => window.RelayIDE.getState().folders.includes("src/renamed-folder")')
            page.reload();page.wait_for_function('() => window.RelayIDE.getState()?.folders.includes("src/renamed-folder")')
            page.locator('[data-folder="src/renamed-folder"] > summary').click(button='right')
            confirm('[data-file-action=delete]')
            page.wait_for_function('() => !window.RelayIDE.getState().folders.includes("src/renamed-folder")')
            report['checks'].append('Create file/folder, edit/save, Save As, rename/move, confirmed and cancelled deletion, folder context menu, refresh persistence')

            page.locator('#fileTree [data-open-file="src/example.py"]').click()
            page.evaluate('() => window.RelayIDE.editor.setValue("answer = 43\\n")')
            page.wait_for_function('() => document.getElementById("saveState").textContent==="Draft retained"')
            page.locator('[data-pane=conversations]').click()
            prompt('#newConversation','Draft switch check')
            page.wait_for_function('() => document.getElementById("conversationList").textContent.includes("Draft switch check")')
            assert page.evaluate('window.RelayIDE.editor.getValue()')=='answer = 43\n'
            page.reload();page.wait_for_function('() => window.RelayIDE.editor?.getValue()==="answer = 43\\n"')
            page.locator('#saveFile').click();page.wait_for_function('() => document.getElementById("saveState").textContent==="Saved"')
            report['checks'].append('Unsaved editor draft retained through conversation switch and browser refresh')

            page.locator('[data-pane=source]').click();page.locator('#stageAll').click()
            page.wait_for_function('() => window.RelayIDE.getState().changes.some(change=>change.staged)')
            page.locator('#stagedDiff').click();page.wait_for_function('() => window.RelayIDE.activeFile==="Working tree.diff"')
            page.locator('#unstageAll').click();page.wait_for_function('() => window.RelayIDE.getState().changes.every(change=>!change.staged)')
            page.locator('#stageAll').click();page.wait_for_function('() => window.RelayIDE.getState().changes.some(change=>change.staged)')
            page.locator('#commitMessage').fill('Management browser source');page.locator('#commitChanges').click()
            page.wait_for_function('() => document.getElementById("commitMessage").value===""')
            page.locator('#branchName').fill('management-check');page.locator('#createBranch').click()
            page.wait_for_function('() => document.getElementById("consoleOutput").textContent.includes("git branch management-check")')
            page.locator('#switchBranch').click();page.wait_for_function('() => window.RelayIDE.getState().branch==="management-check"')
            page.locator('[data-git=log]').click();page.wait_for_function('() => window.RelayIDE.activeFile==="Git history"')
            report['checks'].append('Actual stage/unstage, staged diff, commit, create/switch branch, log, checkpoint comparison')

            worker=enroll('b','Browser Native B')
            assign(worker,False)
            page.wait_for_function('() => document.getElementById("taskState").textContent==="completed"',timeout=30000)
            page.locator('[data-pane=source]').click();page.locator('#compareCheckpoints').click();page.wait_for_function('() => window.RelayIDE.activeFile==="Checkpoint comparison.diff"')
            page.locator('[data-pane=agents]').click()
            tasks=case.client.get('tasks?project_id='+case.project)['tasks'];task=tasks[0]
            assert task['worker_id']==worker and task['requested_worker_id']==worker
            page.locator('[data-inspect-task="'+task['id']+'"]').click()
            page.wait_for_function('() => window.RelayIDE.activeFile==="Agent checkpoint changes.diff"')
            assert page.locator('#timeline .operation-card').count()>0
            page.locator('#timeline .operation-card summary').first.click()
            assert page.locator('#timeline .operation-card').first.evaluate('(node)=>node.open')
            assert 'Last observed' in page.locator('#workers').inner_text()
            page.screenshot(path=str(output/'management-agents.png'),full_page=True)
            report['checks'].append('Add Agent with ready profile; direct task dispatch to exact B; real backend task state, observed activity, expandable tools, Inspect Changes (mock inference)')

            # Queues are deliberately manual; a queued task must not run on reload.
            assign(worker,True)
            queued=case.client.get('tasks?project_id='+case.project)['tasks'][0]
            assert queued['state']=='queued' and queued['requested_worker_id']==worker
            page.reload();page.wait_for_function('() => document.getElementById("taskState").textContent==="queued"')
            page.locator('[data-pane=agents]').click()
            page.locator('[data-task-action="tasks/start"][data-task="'+queued['id']+'"]').click()
            page.wait_for_function('() => document.getElementById("taskState").textContent==="completed"')
            assign(worker,True);cancelled=case.client.get('tasks?project_id='+case.project)['tasks'][0]
            confirm('[data-task-action=cancel][data-task="'+cancelled['id']+'"]')
            page.wait_for_function('() => document.getElementById("taskState").textContent==="cancelled"')
            report['checks'].append('Queue persists across refresh; explicit exact-agent Start; confirmed queued cancellation')

            page.locator('[data-pane=conversations]').click()
            identity=cancelled['conversation_id']
            prompt('[data-rename-conversation="'+identity+'"]','Renamed task conversation')
            page.wait_for_function('() => document.getElementById("conversationList").textContent.includes("Renamed task conversation")')
            confirm('[data-archive-conversation="'+identity+'"]')
            page.wait_for_function('() => document.querySelector("[data-conversation=\\"'+identity+'\\"]").textContent.includes("Archived")')
            page.locator('[data-archive-conversation="'+identity+'"]').click()
            page.wait_for_function('() => !document.querySelector("[data-conversation=\\"'+identity+'\\"]").textContent.includes("Archived")')
            report['checks'].append('Conversation rename/archive/restore with history preservation')

            page.locator('[data-pane=agents]').click()
            settings=page.locator('[data-settings="'+worker+'"]');settings.locator('summary').click()
            settings.locator('[name=display_name]').fill('Renamed Native B');settings.locator('button[type=submit]').click()
            page.wait_for_function('() => document.getElementById("workers").textContent.includes("Renamed Native B")')
            confirm('[data-remove-project="'+worker+'"]')
            page.wait_for_function('(id)=>document.querySelector("[data-assign-worker=\\""+id+"\\"]").disabled',arg=worker)
            confirm('[data-remove-worker="'+worker+'"]')
            page.wait_for_function('(id)=>!document.querySelector("[data-remove-worker=\\""+id+"\\"]")',arg=worker)
            assert case.client.get('tasks/'+task['id']+'/result')['runs']
            report['checks'].append('Agent rename, project authorization removal, enrollment removal; historical runs retained')

            prompt('#newProject','Browser created project')
            page.wait_for_function('() => document.getElementById("statusProject").textContent==="Browser created project"')
            assert not case.server.relay.rows('SELECT 1 FROM human_workspace_locks WHERE project_id=?',(case.project,))
            page.locator('[data-pane=explorer]').click();prompt('#newFile','close_me.py')
            page.wait_for_function('() => window.RelayIDE.activeFile==="close_me.py"')
            page.locator('[data-close-buffer]').click();page.wait_for_function('() => !window.RelayIDE.getState().lock')
            report['checks'].append('Clean reservations released automatically on project switch and last-tab close')
            page.locator('#project').select_option(case.project)
            page.wait_for_function('(id)=>document.getElementById("project").value===id && window.RelayIDE.getState()?.branch==="management-check"',arg=case.project)
            report['checks'].append('New Project creates a real project; project selection returns to the saved workspace')

            async_calls=[]
            def old_overview(route):
                response=route.fetch();data=response.json();data.pop('features',None);route.fulfill(response=response,json=data)
            page.route('**/api/overview',old_overview)
            page.on('request',lambda request:async_calls.append(request.url) if '/api/tasks?' in request.url else None)
            page.reload();page.wait_for_function('() => !!window.RelayIDE?.getState()')
            assert page.locator('#newFolder').is_disabled()
            assert not async_calls
            page.unroute('**/api/overview',old_overview)
            report['checks'].append('Older running backend remains readable; new endpoint controls are disabled until restart')

            before=case.client.get('tasks?project_id='+case.project)['tasks']
            port,origin=case.server.server_port,case.server.public_url
            case.stop();case.server=make_server(case.root/'data',port,'local-owner',origin)
            case.thread=threading.Thread(target=case.server.serve_forever,daemon=True);case.thread.start();case.client.server=case.server
            page.goto(case.server.public_url)
            page.wait_for_function('() => !!window.RelayIDE.getState()')
            assert case.client.get('tasks?project_id='+case.project)['tasks']==before
            assert not page.locator('#recoverLock').is_visible()
            report['checks'].append('Backend restart preserves tasks, histories and existing authenticated Relay session')
            report['browser_errors']=errors
            if errors:raise AssertionError(errors)
            report['verified']=True
            page.screenshot(path=str(output/'management-desktop.png'),full_page=True)
            context.close();browser.close()
    finally:
        (output/'management-browser-report.json').write_text(json.dumps(report,indent=2))
        case.tearDown()
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
