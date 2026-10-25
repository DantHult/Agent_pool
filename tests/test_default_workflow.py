"""Automatic human reservations retain the existing execution/permission guards."""
from pathlib import Path
import contextlib
import io
import os
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from relay.ide import ProjectIDE, WorkspaceConflict
from relay.store import Relay
import test_local_native_dashboard as native_fixture

CLIENT='default-workflow-browser-012345'


class DefaultWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        self.c=self.relay.detail('alice','demo')['conversation']['id']
    def tearDown(self):self.relay.close();self.temp.cleanup()
    def prepare(self):return self.ide.prepare('alice','demo',CLIENT,'session')
    def finish(self):return self.ide.finish('alice','demo',CLIENT,'session',self.c)
    def save(self,path,content,version=None):return self.ide.save('alice','demo',CLIENT,'session',path,version,content,self.c)

    def test_supported_capabilities_default_on_without_environment_activation(self):
        features=self.relay.overview('alice')['features']
        self.assertTrue(features['automatic_workflow']);self.assertTrue(features['agent_management'])
        self.assertEqual(self.relay.rows('SELECT * FROM human_workspace_locks'),[])
        self.assertTrue(self.prepare()['lock']['mine'])
        self.save('immediate.py','answer=42\n')
        self.assertEqual(self.ide.git('alice','demo',CLIENT,'session',['add','.'],self.c)['exit_code'],0)
        self.assertEqual(self.ide.git('alice','demo',CLIENT,'session',['commit','-m','Default workflow'],self.c)['exit_code'],0)
        self.assertTrue(self.finish()['released'])
        self.assertEqual(self.relay.rows('SELECT * FROM human_workspace_locks'),[])

    def test_local_owner_cli_defaults_to_native_without_a_backend_flag(self):
        from relay.server import main
        server=Mock(public_url='http://127.0.0.1:8000')
        server.serve_forever.side_effect=KeyboardInterrupt
        with patch.dict(os.environ,{'RELAY_AUTH_MODE':'local-owner'}),patch('sys.argv',['relay.server','--auth','local-owner']),patch('relay.server.make_server',return_value=server) as create,contextlib.redirect_stdout(io.StringIO()):
            main()
            self.assertEqual(os.environ['RELAY_ENABLE_NATIVE'],'1')
        self.assertEqual(create.call_args.args[2],'local-owner')
        server.server_close.assert_called_once()

    def test_prepare_is_idempotent_and_clean_finish_does_not_create_empty_commits(self):
        first=self.prepare();head=first['checkpoint']
        self.prepare();self.assertEqual(len(self.relay.rows('SELECT * FROM human_workspace_locks')),1)
        self.assertEqual(self.finish()['checkpoint'],head)
        self.assertEqual(self.relay.ws.git(self.relay.ws.path('demo'),'rev-parse','HEAD'),head)
        self.assertEqual(self.relay.rows('SELECT * FROM workspace_checkpoints'),[])
        self.assertFalse(self.finish()['released'])

    def test_finish_checkpoints_saved_work_before_exact_agent_assignment(self):
        self.prepare();self.save('before_agent.py','human_value=7\n')
        finished=self.finish()
        checkpoints=self.relay.rows('SELECT * FROM workspace_checkpoints')
        self.assertEqual(checkpoints[-1]['git_commit'],finished['checkpoint'])
        task=self.relay.mutate('alice','tasks/assign',{'project_id':'demo','worker_id':'codexA','prompt':'Build a password validator'})
        self.assertEqual(task['worker_id'],'codexA')
        self.assertEqual((self.relay.ws.path('demo')/'before_agent.py').read_text(),'human_value=7\n')

    def test_finish_preserves_unsaved_drafts_and_reservation(self):
        self.prepare();self.ide.draft('alice','demo',CLIENT,'session','draft.py',None,'unsaved')
        with self.assertRaises(WorkspaceConflict):self.finish()
        self.assertTrue(self.ide.state('alice','demo',CLIENT,'session')['lock']['mine'])
        self.assertEqual(self.ide.state('alice','demo')['drafts'][0]['content'],'unsaved')
        self.assertFalse((self.relay.ws.path('demo')/'draft.py').exists())

    def test_other_human_and_agent_reservations_are_never_stolen(self):
        self.prepare()
        with self.assertRaises(WorkspaceConflict):self.ide.prepare('bob','demo','other-browser-012345','other-session')
        with self.assertRaises(WorkspaceConflict):self.ide.finish('bob','demo','other-browser-012345','other-session')
        self.finish()
        task=self.relay.mutate('alice','tasks/assign',{'project_id':'demo','worker_id':'codexA','prompt':'Build a password validator'})
        with self.assertRaises(WorkspaceConflict):self.prepare()
        self.assertEqual(self.relay.rows('SELECT task_id FROM workspace_leases'),[{'task_id':task['id']}])

    def test_reauthentication_recovers_only_same_client_expired_session(self):
        self.prepare()
        self.relay.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',('session','alice',time.time(),time.time()+600,time.time()))
        with self.assertRaises(WorkspaceConflict):self.ide.prepare('alice','demo',CLIENT,'replacement-session')
        self.relay.db.execute('UPDATE sessions SET expires_at=0 WHERE digest=?',('session',))
        with self.assertRaises(WorkspaceConflict):self.ide.prepare('alice','demo','different-browser-012345','replacement-session')
        recovered=self.ide.prepare('alice','demo',CLIENT,'replacement-session')
        self.assertTrue(recovered['lock']['mine'])

    def test_viewer_foreign_project_and_protected_files_remain_blocked(self):
        self.relay.db.execute("UPDATE memberships SET role='viewer' WHERE user_id='bob' AND project_id='demo'")
        with self.assertRaises(PermissionError):self.ide.prepare('bob','demo',CLIENT,'viewer-session')
        with self.assertRaises(PermissionError):self.ide.prepare('alice','private',CLIENT,'session')
        self.prepare()
        for path in ('.git/config','../escape','.env','AUTH.JSON'):
            with self.assertRaises((PermissionError,ValueError)):self.save(path,'blocked')

    def test_reservations_and_drafts_survive_restart(self):
        self.prepare();self.ide.draft('alice','demo',CLIENT,'session','draft.py',None,'retained')
        self.relay.close();self.relay=Relay(self.root/'data');self.ide=ProjectIDE(self.relay)
        state=self.prepare()
        self.assertTrue(state['lock']['mine']);self.assertEqual(state['drafts'][0]['content'],'retained')


class DefaultWorkflowHTTPTests(unittest.TestCase):
    def setUp(self):self.fixture=native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_automatic_routes_use_existing_owner_csrf_and_origin_checks(self):
        client=self.fixture.client;ctx={'project_id':self.fixture.project,'client_id':CLIENT,'conversation_id':self.fixture.conversation}
        self.assertTrue(client.get('overview')['features']['automatic_workflow'])
        self.assertEqual(client.request('/api/ide/prepare',ctx,{'X-Relay-CSRF':''})[0],403)
        self.assertEqual(client.request('/api/ide/prepare',ctx,{'Forwarded':'for=127.0.0.1'})[0],403)
        self.assertTrue(client.post('ide/prepare',ctx)['lock']['mine'])
        client.post('ide/save',{**ctx,'path':'automatic.py','content':'value=42\n','version':None})
        self.assertTrue(client.post('ide/finish',ctx)['released'])
        worker=self.fixture.enroll('b')
        task=client.post('tasks/assign',{'project_id':self.fixture.project,'worker_id':worker,'prompt':native_fixture.OBJECTIVE})
        self.assertEqual(self.fixture.wait(task,'completed')['worker_id'],worker)
