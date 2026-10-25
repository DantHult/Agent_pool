"""Editor/Git/conversation integration and real opt-in isolated console tests."""
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.accounts import InviteIdentityProvider
from relay.ide import ProjectIDE, WorkspaceConflict
from relay.native import initialize_profiles
from relay.migrations import BASE,migrate_ide
from relay.server import make_server
from relay.store import Relay
from test_shared_server import Client, PASSWORD

CLIENT = 'browser-client-0123456789'


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.relay = Relay(self.root / 'data')
        self.ide = ProjectIDE(self.relay)
        self.project = 'demo'
        self.conversation = self.relay.detail('alice', self.project)['conversation']['id']
        self.workspace = self.relay.ws.path(self.project)

    def tearDown(self):
        self.relay.close()
        self.temp.cleanup()

    def acquire(self):
        return self.ide.acquire('alice', self.project, CLIENT, 'test-session')

    def save(self, path, content, expected=None):
        return self.ide.save('alice', self.project, CLIENT, 'test-session', path, expected, content, self.conversation)

    def git(self, *args):
        return self.ide.git('alice', self.project, CLIENT, 'test-session', list(args), self.conversation)

    def test_atomic_save_exact_contents_and_optimistic_conflict(self):
        self.acquire()
        before = self.ide.read_file('alice', self.project, 'README.md')
        edited = self.save('README.md', '# Exact Unicode\nHello, 世界\n', before['version'])
        self.assertEqual((self.workspace / 'README.md').read_text(), '# Exact Unicode\nHello, 世界\n')
        self.assertNotEqual(before['version'], edited['version'])
        with self.assertRaises(WorkspaceConflict):
            self.save('README.md', 'stale overwrite', before['version'])
        self.assertEqual(self.ide.read_file('alice', self.project, 'README.md')['content'], edited['content'])
        self.assertFalse(list(self.workspace.glob('.relay-write-*')))

    def test_legacy_conversation_migration_uses_saved_event_activity(self):
        connection=sqlite3.connect(':memory:',isolation_level=None)
        try:
            connection.executescript(BASE+'CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT);')
            connection.execute("INSERT INTO projects VALUES('project','Original project','org')")
            connection.execute("INSERT INTO conversations VALUES('original-id','project')")
            for index,timestamp in enumerate(('2025-01-01T12:00:00Z','2025-02-01T12:00:00Z')):
                connection.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('event-'+str(index),'org','project','original-id',None,index+1,timestamp,None,'prompt','user','Original visible text','{}'))
            migrate_ide(connection)
            self.assertEqual(connection.execute('SELECT id,project_id,title,created_at,updated_at FROM conversations').fetchone(),('original-id','project','Original project','2025-01-01T12:00:00Z','2025-02-01T12:00:00Z'))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
        finally:connection.close()

    def test_save_preserves_executable_permissions(self):
        self.acquire()
        path = self.workspace / 'tool.py'
        path.write_text('print(1)\n');path.chmod(0o755)
        previous = self.ide.read_file('alice', self.project, 'tool.py')
        self.save('tool.py', 'print(2)\n', previous['version'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_new_nested_file_and_missing_version_cannot_overwrite(self):
        self.acquire()
        self.save('src/nested/example.py', 'answer = 42\n')
        with self.assertRaises(WorkspaceConflict):
            self.save('src/nested/example.py', 'answer = 0\n')
        self.assertEqual((self.workspace / 'src/nested/example.py').read_text(), 'answer = 42\n')

    def test_path_traversal_and_credentials_never_read_or_write(self):
        self.acquire()
        for name in ('../outside', '/etc/passwd', 'src/../../outside', 'src\\outside', '.git/config', '.codex/auth.json', 'auth.json', '.env', '.env.local', 'private.pem', '.relay/HANDOFF.md'):
            with self.assertRaises((ValueError, PermissionError)):
                self.save(name, 'blocked')
        for name in ('../outside', '/etc/passwd', '.git/config', '.env', '.codex/auth.json'):
            with self.assertRaises((ValueError, PermissionError)):
                self.ide.read_file('alice', self.project, name)
        self.assertFalse((self.root / 'outside').exists())

    def test_symlink_directory_file_and_hardlink_are_rejected(self):
        self.acquire()
        outside = self.root / 'outside';outside.mkdir();canary = outside / 'canary';canary.write_text('nonsecret')
        (self.workspace / 'linked').symlink_to(outside, target_is_directory=True)
        (self.workspace / 'link.py').symlink_to(canary)
        os.link(canary, self.workspace / 'hard.py')
        for name in ('linked/canary', 'link.py', 'hard.py'):
            with self.assertRaises((ValueError, OSError)):
                self.ide.read_file('alice', self.project, name)
            with self.assertRaises((ValueError, OSError)):
                self.save(name, 'overwrite')
        self.assertEqual(canary.read_text(), 'nonsecret')

    def test_project_membership_viewer_and_other_editor_enforced(self):
        with self.assertRaises(PermissionError):
            self.ide.read_file('alice', 'private', 'README.md')
        self.acquire()
        with self.assertRaises(WorkspaceConflict):
            self.ide.acquire('bob', self.project, 'another-browser-012345', 'other', recover=True)
        self.relay.db.execute("UPDATE memberships SET role='viewer' WHERE user_id='bob' AND project_id='demo'")
        self.assertIn('content', self.ide.read_file('bob', 'demo', 'README.md'))
        with self.assertRaises(PermissionError):
            self.ide.acquire('bob', self.project, 'another-browser-012345', 'other')

    def test_human_reservation_blocks_agent_before_any_side_effect(self):
        self.acquire()
        before = self.relay.ws.git(self.workspace, 'rev-parse', 'HEAD')
        with self.assertRaises(WorkspaceConflict):
            self.relay.mutate('alice', 'prompt', {'conversation_id': self.conversation, 'prompt': 'Build a password validator'})
        self.assertEqual(self.relay.rows('SELECT * FROM tasks'), [])
        self.assertEqual(before, self.relay.ws.git(self.workspace, 'rev-parse', 'HEAD'))

    def test_agent_lease_prevents_file_and_git_writes(self):
        task = self.relay.mutate('alice', 'prompt', {'conversation_id': self.conversation, 'prompt': 'Build a password validator'})
        with self.assertRaises(WorkspaceConflict):self.acquire()
        with self.assertRaises(WorkspaceConflict):self.save('during.py', 'blocked')
        with self.assertRaises(WorkspaceConflict):self.git('add', '.')
        self.assertFalse((self.workspace / 'during.py').exists())
        self.relay.mutate('alice', 'cancel', {'task_id':task['id']})

    def test_drafts_locks_and_conversations_survive_restart_and_recovery(self):
        self.acquire()
        baseline = self.ide.read_file('alice', self.project, 'README.md')
        self.ide.draft('alice', self.project, CLIENT, 'test-session', 'README.md', baseline['version'], 'retained unsaved text')
        second = self.ide.create_conversation('alice', self.project, 'Second conversation')
        with self.assertRaises(WorkspaceConflict):
            self.ide.release('alice', self.project, CLIENT, 'test-session', True)
        self.relay.close();self.relay = Relay(self.root / 'data');self.ide = ProjectIDE(self.relay)
        state = self.ide.state('alice', self.project, CLIENT, 'test-session')
        self.assertTrue(state['lock']['mine']);self.assertEqual(state['drafts'][0]['content'], 'retained unsaved text')
        self.assertEqual(self.relay.detail('alice', self.project, second['id'])['conversation']['id'], second['id'])
        recovered = self.ide.acquire('alice', self.project, 'new-browser-0123456789', 'new-session', True)
        self.assertTrue(recovered['lock']['mine']);self.assertEqual(len(recovered['drafts']), 1)
        with self.assertRaises(WorkspaceConflict):self.save('wrong-browser.py', 'blocked')

    def test_git_status_stage_commit_diff_and_checkpoint_match(self):
        self.acquire()
        self.save('example.py', 'value = 42\n')
        changes = self.ide.state('alice', self.project)['changes']
        self.assertEqual(changes[0]['path'], 'example.py')
        self.assertEqual(self.git('add', 'example.py')['exit_code'], 0)
        self.assertIn('value = 42', self.git('diff', '--staged')['output'])
        self.assertEqual(self.git('commit', '-m', 'Add example')['exit_code'], 0)
        checkpoint = self.relay.rows('SELECT * FROM workspace_checkpoints')[-1]
        self.assertEqual(checkpoint['git_commit'], self.relay.ws.git(self.workspace, 'rev-parse', 'HEAD'))
        self.assertEqual(self.git('status')['exit_code'], 0)
        self.assertIn('Add example', self.git('log')['output'])

    def test_git_does_not_execute_hooks_or_accept_arbitrary_flags(self):
        self.acquire();self.save('a.py', 'value = 1\n');self.git('add', '.')
        hook = self.workspace / '.git/hooks/pre-commit'
        marker = self.root / 'hook-ran'
        hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n');hook.chmod(0o755)
        self.assertEqual(self.git('commit', '-m', 'Safe hook-free commit')['exit_code'], 0)
        self.assertFalse(marker.exists())
        for args in (('reset','--hard'), ('clean','-fd'), ('config','core.hooksPath','/tmp'), ('diff','--output=/tmp/x'), ('log','--format=%G?'), ('commit','--amend')):
            with self.assertRaises(ValueError):self.git(*args)

    def test_git_command_failure_is_real_and_nonzero(self):
        self.acquire()
        result = self.git('commit','-m','Nothing staged')
        self.assertNotEqual(result['exit_code'],0)
        self.assertIn('nothing',result['output'].lower())

    def test_git_diff_does_not_expose_protected_project_credentials(self):
        self.acquire()
        path=self.workspace/'auth.json';path.write_text('{"local":"nonsecret-baseline"}')
        self.relay.ws.git(self.workspace,'add','auth.json')
        self.relay.ws.git(self.workspace,'commit','-qm','Operator fixture only')
        path.write_text('{"local":"PROTECTED-CONTENT-MUST-NOT-RENDER"}')
        self.assertNotIn('PROTECTED-CONTENT-MUST-NOT-RENDER',self.git('diff')['output'])

    def test_worker_assignment_metadata_cannot_reveal_another_project(self):
        conversation=self.relay.detail('bob','private')['conversation']['id']
        task=self.relay.mutate('bob','prompt',{'conversation_id':conversation,'worker_id':'codexB','prompt':'Build a password validator'})
        worker=self.relay.one('SELECT * FROM workers WHERE id=?',('codexB',))
        public=self.relay.public_worker('alice',worker,'demo')
        self.assertEqual(public['assigned_projects'],[])
        self.assertEqual(self.relay.public_worker('bob',worker,'private')['assigned_projects'][0]['id'],'private')
        self.relay.mutate('bob','cancel',{'task_id':task['id']})

    def test_viewer_git_read_does_not_append_conversation_messages(self):
        self.relay.db.execute("UPDATE memberships SET role='viewer' WHERE user_id='bob' AND project_id='demo'")
        before=self.relay.events('bob',self.conversation)['events']
        result=self.ide.git('bob','demo','unused-client','unused-session',['status'],self.conversation)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(self.relay.events('bob',self.conversation)['events'],before)

    def test_wrong_conversation_is_rejected_before_mutating_files_or_git(self):
        self.acquire()
        private=self.relay.detail('bob','private')['conversation']['id']
        head=self.relay.ws.git(self.workspace,'rev-parse','HEAD')
        with self.assertRaises(ValueError):
            self.ide.save('alice','demo',CLIENT,'test-session','wrong.py',None,'blocked',private)
        self.assertFalse((self.workspace/'wrong.py').exists())
        with self.assertRaises(ValueError):
            self.ide.release('alice','demo',CLIENT,'test-session',True,private)
        self.assertEqual(self.relay.ws.git(self.workspace,'rev-parse','HEAD'),head)

    def test_branches_work_and_switch_is_blocked_by_unfinished_task(self):
        self.acquire()
        self.assertEqual(self.git('branch','feature/ide')['exit_code'],0)
        self.assertEqual(self.git('switch','feature/ide')['exit_code'],0)
        self.assertEqual(self.ide.state('alice',self.project)['branch'],'feature/ide')
        self.ide.release('alice',self.project,CLIENT,'test-session',True)
        task=self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'prompt':'Build a password validator'})
        self.relay.mutate('alice','pause',{'task_id':task['id'],'generation':task['generation']})
        self.acquire()
        with self.assertRaises(WorkspaceConflict):self.git('switch','master')

    def test_release_updates_saved_task_context_before_handoff(self):
        task=self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'prompt':'Build a password validator'})
        pending=self.relay.mutate('alice','pause',{'task_id':task['id'],'generation':task['generation']})
        self.assertEqual(pending['state'],'handoff_pending')
        self.acquire();self.save('human.py','human_value = 7\n')
        with self.assertRaises(WorkspaceConflict):self.relay.mutate('alice','resume',{'task_id':task['id'],'generation':pending['generation']})
        released=self.ide.release('alice',self.project,CLIENT,'test-session',True,self.conversation)
        context=json.loads(self.relay.rows('SELECT data FROM contexts WHERE task_id=? ORDER BY version DESC',(task['id'],))[0]['data'])
        self.assertEqual(context['checkpoint'],released['checkpoint']);self.assertIn('human.py',context['files_changed'])
        self.assertFalse(self.ide.state('alice',self.project)['lock'])

    def test_conversations_are_separate_but_only_one_project_task_runs(self):
        second=self.ide.create_conversation('alice',self.project,'Separate conversation')
        other=self.ide.create_conversation('bob','private','Private thread')
        with self.assertRaises(ValueError):self.relay.detail('alice',self.project,other['id'])
        task=self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'prompt':'Build a password validator'})
        with self.assertRaises(ValueError):self.relay.mutate('alice','prompt',{'conversation_id':second['id'],'prompt':'Build a password validator'})
        self.assertEqual(self.relay.events('alice',second['id'])['events'],[])
        self.assertTrue(self.relay.events('alice',self.conversation)['events'])
        self.relay.mutate('alice','cancel',{'task_id':task['id']})


class EditorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=make_server(self.temp.name,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=Client(self.server)
        self.client.post('login',{'user_id':'alice'})

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.temp.cleanup()

    def test_editor_api_auth_scope_conflict_and_large_source_save(self):
        self.assertEqual(Client(self.server).request('/api/ide/state?project_id=demo')[0],403)
        self.assertEqual(self.client.request('/api/ide/state?project_id=private')[0],403)
        self.client.post('ide/lock',{'project_id':'demo','client_id':CLIENT})
        text='# editor source\n' * 2000
        result=self.client.post('ide/save',{'project_id':'demo','client_id':CLIENT,'path':'large.py','version':None,'content':text})
        self.assertEqual(self.client.get('ide/file?project_id=demo&path=large.py')['content'],text)
        self.assertEqual(self.client.request('/api/ide/save',{'project_id':'demo','client_id':CLIENT,'path':'large.py','version':'old','content':'stale'})[0],409)
        self.assertTrue(result['version'])

    def test_console_disabled_in_demo_and_static_assets_scoped(self):
        self.assertEqual(self.client.request('/api/ide/console/open',{'project_id':'demo','client_id':CLIENT,'authorize':True})[0],403)
        self.assertEqual(self.client.request('/api/ide/console?session_id=anything')[0],403)
        self.assertEqual(self.client.request('/vendor/../../relay/server.py')[0],404)
        self.assertEqual(self.client.request('/vendor/monaco/editor.js')[0],200)


@unittest.skipUnless(platform.system()=='Darwin' and os.environ.get('RELAY_RUN_SANDBOX_TESTS')=='1','Opt-in actual App Server sandbox; no authentication or model request')
class RealConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.environment=patch.dict(os.environ,{'RELAY_NATIVE_ROOT':str(self.root/'native'),'RELAY_PUBLIC_URL':''});self.environment.start();initialize_profiles()
        relay=Relay(self.root/'data',demo=False)
        self.owner=InviteIdentityProvider(relay).bootstrap('owner@relay.invalid','IDE Owner',PASSWORD,local_owner=True)['id'];relay.close()
        self.server=make_server(self.root/'data',0,'local-owner')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=Client(self.server);self.client.login('owner@relay.invalid')
        self.project=self.client.post('projects',{'name':'Actual isolated console'})['id']
        self.client.post('ide/lock',{'project_id':self.project,'client_id':CLIENT})

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.environment.stop();self.temp.cleanup()

    def open(self):
        item=self.client.post('ide/console/open',{'project_id':self.project,'client_id':CLIENT,'authorize':True})
        return self.wait(item['id'],{'ready'})

    def wait(self,identity,states):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            item=self.client.get('ide/console?session_id='+identity)
            if item['state'] in states:return item
            if item['state']=='failed':self.fail(str(item))
            time.sleep(.05)
        self.fail('Console did not reach '+str(states)+': '+str(item))

    def test_real_stream_stdin_command_failure_and_cancellation(self):
        item=self.open();identity=item['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -u -c 'print(\"ready-input\",flush=True); print(\"echo:\"+input())'"})
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            live=self.client.get('ide/console?session_id='+identity)
            if 'ready-input' in live['output']:break
            time.sleep(.05)
        self.assertIn('ready-input',live['output'])
        self.client.post('ide/console/input',{'session_id':identity,'input':'hello\n'})
        done=self.wait(identity,{'finished'});self.assertEqual(done['exit_code'],0);self.assertIn('echo:hello',done['output'])
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -c 'raise SystemExit(7)'"})
        self.assertEqual(self.wait(identity,{'finished'})['exit_code'],7)
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -c 'import time; time.sleep(60)'"})
        self.client.post('ide/console/cancel',{'session_id':identity});self.wait(identity,{'finished'})

    def test_stdin_sent_immediately_is_scoped_to_the_started_process(self):
        identity=self.open()['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -u -c 'print(input())'"})
        self.client.post('ide/console/input',{'session_id':identity,'input':'early-input\n'})
        done=self.wait(identity,{'finished'})
        self.assertEqual(done['exit_code'],0);self.assertIn('early-input',done['output'])

    def test_actual_isolation_and_explicit_apply_checkpoint(self):
        identity=self.open()['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -c 'from pathlib import Path; Path(\"created.py\").write_text(\"answer = 42\\n\")'"})
        self.assertEqual(self.wait(identity,{'finished'})['exit_code'],0)
        canonical=self.server.relay.ws.path(self.project)
        self.assertFalse((canonical/'created.py').exists())
        result=self.client.post('ide/console/apply',{'session_id':identity})
        self.assertTrue(result['applied']);self.assertEqual((canonical/'created.py').read_text(),'answer = 42\n')
        self.assertEqual(result['checkpoint'],self.server.relay.ws.git(canonical,'rev-parse','HEAD'))

    def test_automatic_finish_closes_unchanged_console_and_releases(self):
        identity=self.open()['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':'python3 --version'})
        self.assertEqual(self.wait(identity,{'finished'})['exit_code'],0)
        result=self.client.post('ide/finish',{'project_id':self.project,'client_id':CLIENT})
        self.assertTrue(result['released'])
        self.assertTrue(self.client.get('ide/console?session_id='+identity)['closed'])
        self.assertIsNone(self.client.get('ide/state?project_id='+self.project)['lock'])

    def test_automatic_finish_refuses_running_or_unpublished_console_work(self):
        identity=self.open()['id'];ctx={'project_id':self.project,'client_id':CLIENT}
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -u -c 'from pathlib import Path; import time; Path(\"pending.py\").write_text(\"value=42\\n\"); print(\"waiting\",flush=True); time.sleep(60)'"})
        self.assertEqual(self.client.request('/api/ide/finish',ctx)[0],409)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if 'waiting' in self.client.get('ide/console?session_id='+identity)['output']:break
            time.sleep(.05)
        self.client.post('ide/console/cancel',{'session_id':identity});self.wait(identity,{'finished'})
        self.assertEqual(self.client.request('/api/ide/finish',ctx)[0],409)
        self.assertIsNotNone(self.client.get('ide/state?project_id='+self.project)['lock'])
        self.assertFalse((self.server.relay.ws.path(self.project)/'pending.py').exists())

    def test_next_run_refreshes_clean_snapshot_after_editor_save(self):
        first=self.open()['id'];ctx={'project_id':self.project,'client_id':CLIENT,'authorize':True}
        self.client.post('ide/save',{**ctx,'path':'latest.py','content':'value=73\n','version':None})
        replacement=self.client.post('ide/console/ensure',ctx)
        self.assertNotEqual(replacement['id'],first)
        self.wait(replacement['id'],{'ready'})
        self.client.post('ide/console/run',{'session_id':replacement['id'],'command':"python3 -c 'import latest; print(latest.value)'"})
        done=self.wait(replacement['id'],{'finished'})
        self.assertEqual(done['exit_code'],0);self.assertIn('73',done['output'])
        self.assertTrue(self.client.get('ide/console?session_id='+first)['closed'])
        # A confirmed stopped transport must be replaced on the next explicit
        # Run. Never reuse it or replay the interrupted command automatically.
        self.server.ide.console.sessions[replacement['id']]['client'].close()
        recovered=self.client.post('ide/console/ensure',ctx)
        self.assertNotEqual(recovered['id'],replacement['id'])
        self.wait(recovered['id'],{'ready'})
        self.client.post('ide/console/run',{'session_id':recovered['id'],'command':"python3 -c 'print(\"recovered-command\")'"})
        final=self.wait(recovered['id'],{'finished'})
        self.assertEqual(final['exit_code'],0);self.assertIn('recovered-command',final['output'])

    def test_sensitive_files_git_metadata_network_and_shell_denied(self):
        canary=self.root/'nonsecret-canary';canary.write_text('must-not-read')
        canonical=self.server.relay.ws.path(self.project)
        (canonical/'auth.json').write_text('nonsecret-protected-fixture')
        self.server.relay.ws.git(canonical,'add','auth.json')
        self.server.relay.ws.git(canonical,'commit','-qm','Protected fixture; never copy to console')
        identity=self.open()['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -c 'from pathlib import Path; import subprocess; assert not Path(\"auth.json\").exists(); assert b\"nonsecret-protected-fixture\" not in subprocess.check_output([\"git\",\"log\",\"-p\"]); print(\"sanitized-snapshot-ok\")'"})
        safe=self.wait(identity,{'finished'});self.assertEqual(safe['exit_code'],0);self.assertIn('sanitized-snapshot-ok',safe['output'])
        for command in ["python3 -c 'from pathlib import Path; print(Path(\""+str(canary)+"\").read_text())'", "python3 -c 'from pathlib import Path; Path(\".git/config\").write_text(\"blocked\")'", "python3 -c 'import socket; socket.create_connection((\"127.0.0.1\",9000),timeout=1)'"]:
            self.client.post('ide/console/run',{'session_id':identity,'command':command})
            done=self.wait(identity,{'finished'});self.assertNotEqual(done['exit_code'],0);self.assertNotIn('must-not-read',done['output'])
        for command in ('/bin/sh','sh -c whoami','git config core.hooksPath /tmp','git add .'):
            self.assertEqual(self.client.request('/api/ide/console/run',{'session_id':identity,'command':command})[0],400)
        self.assertEqual(self.client.request('/api/ide/console/run',{'session_id':identity,'command':'python3 --version','cwd':'../'})[0],400)
        self.assertEqual(Client(self.server).request('/api/ide/console?session_id='+identity)[0],401)
        self.assertEqual(self.client.request('/api/ide/console?session_id='+identity,headers={'X-Forwarded-For':'127.0.0.1'})[0],403)

    def test_apply_refuses_concurrent_editor_changes_and_unsaved_drafts(self):
        identity=self.open()['id']
        self.client.post('ide/console/run',{'session_id':identity,'command':"python3 -c 'from pathlib import Path; Path(\"console.py\").write_text(\"console = True\\n\")'"})
        self.wait(identity,{'finished'})
        self.client.post('ide/save',{'project_id':self.project,'client_id':CLIENT,'path':'human.py','content':'human = True\n','version':None})
        self.assertEqual(self.client.request('/api/ide/console/apply',{'session_id':identity})[0],409)
        canonical=self.server.relay.ws.path(self.project)
        self.assertFalse((canonical/'console.py').exists());self.assertEqual((canonical/'human.py').read_text(),'human = True\n')
        self.assertEqual(self.client.request('/api/ide/release',{'project_id':self.project,'client_id':CLIENT,'checkpoint':True})[0],409)

    def test_explicit_authorization_csrf_session_binding_and_logout_cleanup(self):
        self.assertEqual(self.client.request('/api/ide/console/open',{'project_id':self.project,'client_id':CLIENT})[0],403)
        self.assertEqual(self.client.request('/api/ide/console/open',{'project_id':self.project,'client_id':CLIENT,'authorize':True},headers={'X-Relay-CSRF':''})[0],403)
        identity=self.open()['id']
        second=Client(self.server);second.login('owner@relay.invalid')
        self.assertEqual(second.request('/api/ide/console?session_id='+identity)[0],403)
        self.client.post('logout',{})
        self.assertTrue(self.server.ide.console.sessions[identity]['closed'])


if __name__=='__main__':unittest.main()
