"""Explorer and explicit agent management regressions; inference is mocked."""
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from relay.ide import ProjectIDE, WorkspaceConflict
from relay.store import Relay
import test_local_native_dashboard as native_fixture

CLIENT = 'management-browser-01234567'
PROMPT = 'Build a password validator and tests'


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.relay = Relay(self.root/'data')
        self.ide = ProjectIDE(self.relay)
        self.c = self.relay.detail('alice', 'demo')['conversation']['id']
        self.ws = self.relay.ws.path('demo')

    def tearDown(self):
        self.relay.close();self.temp.cleanup()

    def lock(self):self.ide.acquire('alice', 'demo', CLIENT, 'session')
    def entry(self, path):return self.ide.files.inspect('alice', 'demo', path)
    def op(self, action, path, **data):
        return self.ide.files.mutate('alice', 'demo', CLIENT, 'session', {'action': action, 'path': path, 'conversation_id': self.c, **data})
    def save(self, path, content='', expected=None):return self.ide.save('alice','demo',CLIENT,'session',path,expected,content,self.c)
    def assign(self, worker='codexA', queue=False, **data):
        return self.relay.mutate('alice','tasks/assign',{'project_id':'demo','worker_id':worker,'prompt':PROMPT,'queue':queue,**data})

    def test_empty_nested_folders_files_rename_move_delete_and_restart(self):
        self.lock();self.op('mkdir','src');self.op('mkdir','src/empty');self.op('mkdir','other')
        self.save('src/code.py','value = 42\n')
        self.assertIn('src/empty',self.ide.state('alice','demo')['folders'])
        before=self.entry('src')
        self.op('rename','src',target='other/moved',version=before['version'])
        self.assertEqual((self.ws/'other/moved/code.py').read_text(),'value = 42\n')
        self.assertIn('other/moved/empty',self.ide.state('alice','demo')['folders'])
        self.relay.close();self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        self.assertIn('other/moved/empty',self.ide.state('alice','demo')['folders'])
        moved=self.entry('other/moved')
        self.op('delete','other/moved',version=moved['version'],confirmed=True)
        self.assertFalse((self.ws/'other/moved').exists())
        self.assertTrue((self.ws/'other').is_dir())

    def test_rename_delete_require_current_version_and_no_overwrite(self):
        self.lock();first=self.save('a.py','a\n');self.save('b.py','b\n')
        with self.assertRaises(WorkspaceConflict):self.op('rename','a.py',target='b.py',version=first['version'])
        self.assertEqual((self.ws/'b.py').read_text(),'b\n')
        self.save('a.py','changed\n',first['version'])
        with self.assertRaises(WorkspaceConflict):self.op('delete','a.py',version=first['version'],confirmed=True)
        before=self.entry('a.py')
        with self.assertRaises(ValueError):self.op('delete','a.py',version=before['version'])
        self.op('rename','a.py',target='renamed.py',version=before['version'])
        self.assertFalse((self.ws/'a.py').exists())

    def test_folder_conflict_detects_nested_changes_and_empty_destination(self):
        self.lock();self.op('mkdir','folder');self.op('mkdir','destination')
        before=self.entry('folder');self.save('folder/new','new')
        with self.assertRaises(WorkspaceConflict):self.op('delete','folder',version=before['version'],confirmed=True)
        with self.assertRaises(WorkspaceConflict):self.op('rename','folder',target='destination',version=self.entry('folder')['version'])
        with self.assertRaises(ValueError):self.op('rename','folder',target='folder/child',version=self.entry('folder')['version'])

    def test_protected_traversal_symlink_and_hardlink_operations_reject(self):
        self.lock();outside=self.root/'outside';outside.mkdir();canary=outside/'canary';canary.write_text('safe')
        (self.ws/'link').symlink_to(outside,target_is_directory=True)
        os.link(canary,self.ws/'hard')
        for path in ('../outside','/tmp/outside','.git','.GIT/config','.relay','.ReLaY/HANDOFF.md','auth.json','AUTH.JSON','.env','.ENV.local','link/canary','link','hard'):
            with self.assertRaises((ValueError,PermissionError,OSError)):self.op('delete',path,version='bad',confirmed=True)
        self.op('mkdir','secrets');(self.ws/'secrets/.env').write_text('nonsecret-canary')
        with self.assertRaises(PermissionError):self.entry('secrets')
        self.assertEqual(canary.read_text(),'safe')
        self.assertTrue((self.ws/'secrets/.env').exists())

    def test_drafts_and_other_editors_block_structural_changes(self):
        self.lock();saved=self.save('draft.py','original')
        self.ide.draft('alice','demo',CLIENT,'session','draft.py',saved['version'],'unsaved')
        with self.assertRaises(WorkspaceConflict):self.op('rename','draft.py',target='new.py',version=saved['version'])
        with self.assertRaises(WorkspaceConflict):self.op('rename','DRAFT.py',target='new.py',version=saved['version'])
        with self.assertRaises(WorkspaceConflict):self.ide.files.mutate('bob','demo','another-browser-012345','other',{'action':'mkdir','path':'no'})
        self.assertEqual(self.ide.state('alice','demo')['drafts'][0]['content'],'unsaved')

    def test_file_operations_scope_checked_before_mutation(self):
        self.lock()
        foreign=self.relay.detail('bob','private')['conversation']['id']
        with self.assertRaises(ValueError):self.op('mkdir','bad-scope',conversation_id=foreign)
        self.assertFalse((self.ws/'bad-scope').exists())
        with self.assertRaises(PermissionError):self.ide.files.inspect('alice','private','README.md')

    def test_save_as_is_exclusive_and_preserves_original_draft(self):
        self.lock();original=self.ide.read_file('alice','demo','README.md')
        self.ide.draft('alice','demo',CLIENT,'session','README.md',original['version'],'copy text')
        self.save('copy.md','copy text')
        self.assertEqual((self.ws/'README.md').read_text(),original['content'])
        with self.assertRaises(WorkspaceConflict):self.save('copy.md','overwrite')
        self.assertEqual(len(self.ide.state('alice','demo')['drafts']),1)

    def test_direct_assignment_exact_worker_and_explicit_queue_dispatch(self):
        first=self.assign('codexA');second=self.assign('codexB',queue=True)
        self.assertEqual(first['worker_id'],'codexA');self.assertEqual(second['requested_worker_id'],'codexB')
        chosen=next(w for w in self.relay.workers('alice','demo')['workers'] if w['id']=='codexB')
        self.assertTrue(chosen['eligible']);self.assertFalse(chosen['can_accept_task'])
        self.assertNotEqual(first['conversation_id'],second['conversation_id'])
        self.assertEqual(second['state'],'queued');self.assertIsNone(second['worker_id'])
        with self.assertRaises(WorkspaceConflict):self.relay.mutate('alice','tasks/start',{'task_id':second['id']})
        self.relay.mutate('alice','cancel',{'task_id':first['id']})
        self.assertEqual(self.relay.task('alice',second['id'])['state'],'queued')
        started=self.relay.mutate('alice','tasks/start',{'task_id':second['id']})
        self.assertEqual(started['worker_id'],'codexB')
        self.assertEqual(self.relay.rows("SELECT worker_id FROM assignments WHERE task_id=?",(second['id'],)),[{'worker_id':'codexB'}])

    def test_queued_cancel_never_checkpoints_another_writer(self):
        first=self.assign();queued=self.assign('codexB',queue=True)
        checkpoints=self.relay.rows('SELECT * FROM workspace_checkpoints')
        cancelled=self.relay.mutate('alice','cancel',{'task_id':queued['id']})
        self.assertEqual(cancelled['state'],'cancelled')
        self.assertEqual(checkpoints,self.relay.rows('SELECT * FROM workspace_checkpoints'))
        self.assertEqual(self.relay.rows('SELECT task_id FROM workspace_leases'),[{'task_id':first['id']}])

    def test_unavailable_chosen_agent_errors_without_fallback(self):
        self.relay.mutate('alice','workers/configure',{'worker_id':'codexA','state':'offline'})
        with self.assertRaises(WorkspaceConflict):self.assign('codexA')
        self.assertEqual(self.relay.rows('SELECT * FROM tasks'),[])
        queued=self.assign('codexA',queue=True)
        with self.assertRaises(PermissionError):self.relay.mutate('alice','tasks/start',{'task_id':queued['id']})
        self.assertEqual(self.relay.rows('SELECT * FROM assignments'),[])

    def test_queue_survives_restart_without_automatic_execution(self):
        queued=self.assign('codexB',queue=True)
        self.relay.close();self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        self.assertEqual(self.relay.task('alice',queued['id'])['requested_worker_id'],'codexB')
        self.assertEqual(self.relay.task('alice',queued['id'])['state'],'queued')
        self.assertEqual(self.relay.rows('SELECT * FROM assignments'),[])

    def test_schema_seven_upgrade_preserves_history_grants_and_conversation_ids(self):
        task=self.assign();self.relay.mutate('alice','cancel',{'task_id':task['id']})
        queued=self.assign('codexB',queue=True)
        before={table:self.relay.rows('SELECT * FROM '+table) for table in ('events','assignments','workspace_checkpoints','worker_projects','contexts')}
        conversation_ids=[row['id'] for row in self.ide.conversations('alice','demo')['conversations']]
        self.relay.close()
        db=sqlite3.connect(self.root/'data/relay.sqlite',isolation_level=None)
        db.execute('DROP INDEX one_native_profile')
        db.execute('ALTER TABLE tasks DROP COLUMN requested_worker_id')
        db.execute('ALTER TABLE workers DROP COLUMN removed_at')
        db.execute('ALTER TABLE conversations DROP COLUMN archived_at')
        db.execute("CREATE UNIQUE INDEX one_native_profile ON workers(native_profile) WHERE execution_mode='native'")
        db.execute('DELETE FROM schema_migrations WHERE version=8');db.close()
        self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        self.assertEqual(before,{table:self.relay.rows('SELECT * FROM '+table) for table in before})
        self.assertEqual(conversation_ids,[row['id'] for row in self.ide.conversations('alice','demo')['conversations']])
        self.assertEqual(self.relay.task('alice',task['id'])['requested_worker_id'],'codexA')
        self.assertEqual(self.relay.task('alice',queued['id'])['requested_worker_id'],'codexB')
        self.assertEqual(self.relay.task('alice',queued['id'])['state'],'queued')

    def test_worker_remove_requires_owner_confirmation_and_no_unfinished_tasks(self):
        active=self.assign()
        with self.assertRaises(WorkspaceConflict):self.relay.mutate('alice','workers/remove',{'worker_id':'codexA','confirmed':True})
        self.relay.mutate('alice','cancel',{'task_id':active['id']})
        before=self.relay.rows('SELECT * FROM events')
        with self.assertRaises(ValueError):self.relay.mutate('alice','workers/remove',{'worker_id':'codexA'})
        with self.assertRaises(PermissionError):self.relay.mutate('bob','workers/remove',{'worker_id':'codexA','confirmed':True})
        self.relay.mutate('alice','workers/remove',{'worker_id':'codexA','confirmed':True})
        self.assertEqual(before,self.relay.rows('SELECT * FROM events'))
        self.assertNotIn('codexA',[w['id'] for w in self.relay.workers('alice','demo')['workers']])
        with self.assertRaises(PermissionError):self.assign('codexA',queue=True)

    def test_authorization_cannot_leak_other_project_tasks_or_activity(self):
        private=self.relay.mutate('bob','tasks/assign',{'project_id':'private','worker_id':'codexB','prompt':PROMPT})
        observation=self.relay.management.observation('alice','codexB')
        self.assertIsNone(observation['latest_task'])
        with self.assertRaises(PermissionError):self.relay.management.tasks('alice','private')
        with self.assertRaises(PermissionError):self.relay.management.review('alice','demo',private['id'])
        self.assertNotIn(private['objective'],json.dumps(observation))

    def test_conversation_rename_archive_restore_and_participants_persist(self):
        task=self.assign()
        with self.assertRaises(WorkspaceConflict):self.ide.update_conversation('alice','demo',task['conversation_id'],{'archived':True,'confirmed':True})
        self.relay.mutate('alice','cancel',{'task_id':task['id']})
        identity=task['conversation_id']
        self.ide.update_conversation('alice','demo',identity,{'title':'Renamed conversation'})
        self.ide.update_conversation('alice','demo',identity,{'archived':True,'confirmed':True})
        with self.assertRaises(ValueError):self.assign(conversation_id=identity)
        self.relay.close();self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        row=next(c for c in self.ide.conversations('alice','demo')['conversations'] if c['id']==identity)
        self.assertEqual(row['title'],'Renamed conversation');self.assertTrue(row['archived_at'])
        self.assertEqual(row['participants'][0]['id'],'codexA')
        self.ide.update_conversation('alice','demo',identity,{'archived':False})
        self.assertIsNone(self.ide.conversation('alice','demo',identity)['archived_at'])

    def test_unstage_preserves_files_and_checkpoint_compare_is_scoped(self):
        self.lock();self.save('new.py','value = 7\n')
        def git(*args):return self.ide.git('alice','demo',CLIENT,'session',list(args),self.c)
        git('add','.');self.assertTrue(any(c['staged'] for c in self.ide.changes('demo')))
        git('unstage','.');self.assertFalse(any(c['staged'] for c in self.ide.changes('demo')))
        self.assertEqual((self.ws/'new.py').read_text(),'value = 7\n')
        self.ide.release('alice','demo',CLIENT,'session',True,self.c)
        self.lock();self.save('second.py','value = 8\n');self.ide.release('alice','demo',CLIENT,'session',True,self.c)
        checkpoints=self.relay.rows('SELECT git_commit FROM workspace_checkpoints WHERE project_id=? ORDER BY rowid',('demo',))
        review=self.relay.management.review('alice','demo',before=checkpoints[0]['git_commit'],after=checkpoints[-1]['git_commit'])
        self.assertIn('second.py',review['files']);self.assertIn('+value = 8',review['diff'])
        with self.assertRaises(ValueError):self.relay.management.review('alice','demo',before='HEAD',after='HEAD')
        with self.assertRaises(ValueError):git('merge','other')


class NativeManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
        self.client,self.project=self.fixture.client,self.fixture.project
    def tearDown(self):self.fixture.tearDown()

    def test_direct_native_chosen_worker_real_state_fixture_and_review(self):
        worker=self.fixture.enroll('b')
        task=self.client.post('tasks/assign',{'project_id':self.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE})
        done=self.fixture.wait(task,'completed')
        self.assertEqual(done['worker_id'],worker)
        worker_row=self.client.get('workers?project_id='+self.project)['workers'][0]
        self.assertEqual(worker_row['observation']['latest_task']['id'],task['id'])
        self.assertTrue(worker_row['observation']['last_action']['timestamp'])
        review=self.client.get('ide/review?project_id='+self.project+'&task_id='+task['id'])
        self.assertIn('native_tools.py',review['files'])
        self.assertIn('native_tools.py',review['diff'])
        self.assertEqual(self.client.request('/api/tasks/assign',{'project_id':self.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE},{'X-Relay-CSRF':''})[0],403)

    def test_native_remove_keeps_auth_home_history_and_allows_explicit_reenroll(self):
        worker=self.fixture.enroll('b')
        task=self.client.post('tasks/assign',{'project_id':self.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE})
        self.fixture.wait(task,'completed')
        from relay.native import profile_paths
        home,_=profile_paths('b');before=sorted(p.name for p in home.iterdir())
        self.client.post('workers/remove',{'worker_id':worker,'confirmed':True})
        self.assertEqual(before,sorted(p.name for p in home.iterdir()))
        self.assertTrue(self.client.get('tasks/'+task['id']+'/result')['runs'])
        replacement=self.fixture.enroll('b');self.assertNotEqual(replacement,worker)
        self.assertEqual(self.client.request('/api/workers/enroll',{'display_name':'Duplicate','execution_mode':'native','native_profile':'b','project_ids':[self.project]})[0],400)

    def test_http_explorer_routes_and_confirmation(self):
        c=self.fixture.conversation
        ctx={'project_id':self.project,'client_id':CLIENT,'conversation_id':c}
        self.client.post('ide/lock',ctx)
        self.client.post('ide/files',{**ctx,'action':'mkdir','path':'nested'})
        self.client.post('ide/save',{**ctx,'path':'nested/code.py','content':'value=42\n','version':None})
        entry=self.client.get('ide/entry?project_id='+self.project+'&path=nested/code.py')
        self.client.post('ide/files',{**ctx,'action':'rename','path':'nested/code.py','target':'nested/moved.py','version':entry['version']})
        self.assertEqual(self.client.request('/api/ide/files',{**ctx,'action':'delete','path':'nested/moved.py','version':entry['version']})[0],400)
        self.client.post('ide/files',{**ctx,'action':'delete','path':'nested/moved.py','version':entry['version'],'confirmed':True})
        self.assertIn('nested',self.client.get('ide/state?project_id='+self.project)['folders'])

    def test_simultaneous_assignments_retain_single_project_writer(self):
        from relay.native import close_native_sessions,profile_paths
        close_native_sessions()
        for profile in ('a','b'):
            home,_=profile_paths(profile);(home/'scenario.json').write_text('{"delay":3,"complete":true}')
        workers=[self.fixture.enroll(profile) for profile in ('a','b')]
        barrier=threading.Barrier(2);results=[]
        def submit(worker):
            barrier.wait()
            results.append(self.client.request('/api/tasks/assign',{'project_id':self.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE}))
        threads=[threading.Thread(target=submit,args=(worker,)) for worker in workers]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertEqual(sorted(result[0] for result in results),[200,409])
        self.assertEqual(len(self.fixture.server.relay.rows('SELECT * FROM workspace_leases')),1)
        winner=next(result[1] for result in results if result[0]==200)
        self.client.post('cancel',{'task_id':winner['id']});self.fixture.wait(winner,'cancelled')

    def test_direct_task_pause_and_resume_remains_on_selected_worker(self):
        from relay.native import close_native_sessions,profile_paths
        close_native_sessions();home,_=profile_paths('b');(home/'scenario.json').write_text('{"delay":2,"complete":true}')
        worker=self.fixture.enroll('b')
        task=self.client.post('tasks/assign',{'project_id':self.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE})
        self.client.post('pause',{'task_id':task['id'],'generation':task['generation']})
        paused=self.fixture.wait(task,'handoff_pending')
        self.assertEqual(self.client.request('/api/workers/remove',{'worker_id':worker,'confirmed':True})[0],409)
        self.client.post('resume',{'task_id':task['id'],'generation':paused['generation']})
        done=self.fixture.wait(task,'completed')
        self.assertEqual(done['worker_id'],worker)
        self.assertEqual({row['worker_id'] for row in self.client.get('tasks/'+task['id']+'/result')['runs']},{worker})
