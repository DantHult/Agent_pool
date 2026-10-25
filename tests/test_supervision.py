"""Mock protocol tests; these do not establish real Codex model execution."""
import json
import os
import time
import unittest

from relay.native import close_native_sessions,profile_paths,session_for
from relay.supervision import decision
import test_native as native_fixture
from test_native import PROMPT
import test_local_native_dashboard as dashboard_fixture


class SupervisionTests(unittest.TestCase):
    setUp=native_fixture.NativeAdapterTests.setUp
    tearDown=native_fixture.NativeAdapterTests.tearDown
    wait=native_fixture.NativeAdapterTests.wait

    def scenario(self,value):
        close_native_sessions();home,_=profile_paths('a');(home/'scenario.json').write_text(json.dumps(value));return home

    def start(self):
        return self.relay.mutate('alice','prompt',{'conversation_id':self.conversation,'worker_id':self.workers[0],'execution_mode':'native','prompt':PROMPT})

    def test_same_worker_and_thread_continues_beyond_checkpoint(self):
        home=self.scenario({'continuous':2});task=self.start();done=self.wait(task,'completed')
        turns=[json.loads(row) for row in (home/'turns.jsonl').read_text().splitlines()]
        self.assertEqual(len(turns),2);self.assertEqual(len({row['thread'] for row in turns}),1)
        self.assertIn(PROMPT,turns[1]['text']);self.assertIn('completed_work',turns[1]['text'])
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?',(task['id'],))),1)
        checkpoints=self.relay.rows('SELECT * FROM workspace_checkpoints WHERE task_id=?',(task['id'],))
        self.assertTrue(any(row['reason']=='Intermediate native progress' for row in checkpoints))
        self.assertGreaterEqual(len(checkpoints),3);self.assertGreater(done['execution_phase'],0)
        self.assertFalse(self.relay.rows('SELECT * FROM handoffs WHERE task_id=?',(task['id'],)))
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
        self.assertIn('test_native_tools.py',self.relay.ws.inspect('demo')['files'])
        observed=self.relay.management.observation('alice',self.workers[0])
        self.assertEqual(observed['phase'],'completed');self.assertIsNone(observed['executing_command'])
        self.assertEqual(json.loads(observed['last_tool_result']['metadata'])['exit_code'],0)

    def test_no_progress_is_bounded_without_a_total_turn_limit(self):
        home=self.scenario({'continuous':100});task=self.start();self.wait(task,'handoff_pending')
        turns=(home/'turns.jsonl').read_text().splitlines();self.assertLessEqual(len(turns),5)
        run=self.relay.one('SELECT * FROM execution_runs WHERE task_id=?',(task['id'],))
        self.assertEqual(run['error_category'],'no_progress');self.assertEqual(run['state'],'blocked')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))

    def test_provider_limit_after_checkpoint_never_dispatches_another_worker(self):
        home=self.scenario({'continuous':3,'quota_after':1});task=self.start();self.wait(task,'handoff_pending')
        self.assertEqual(len((home/'turns.jsonl').read_text().splitlines()),2)
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?',(task['id'],))),1)
        self.assertEqual(self.relay.one('SELECT state FROM workers WHERE id=?',(self.workers[0],))['state'],'quota_blocked')
        self.assertEqual(self.relay.management.observation('alice',self.workers[0])['phase'],'blocked')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))

    def test_ambiguous_completion_and_authorization_stop_without_replay(self):
        home=self.scenario({'uncertain':True});task=self.start();self.wait(task,'handoff_pending')
        self.assertEqual(len((home/'turns.jsonl').read_text().splitlines()),1)
        self.assertEqual(self.relay.management.observation('alice',self.workers[0])['phase'],'blocked')
        self.assertEqual(decision({'completion_state':'needs_authorization','pending_steps':['external access']}),'needs_authorization')
        self.assertEqual(decision({'summary':'done'}),'uncertain')
        self.assertEqual(decision({'completion_state':'completed','completed_work':['x'],'acceptance_evidence':['test passed'],'pending_steps':['unfinished']}),'continue')
        self.assertEqual(decision({'completion_state':'continue','errors':['1 test failed'],'pending_steps':['Fix the failing test']}),'continue')
        self.assertEqual(decision({'completion_state':'continue','errors':['Dependency missing'],'pending_steps':[]}),'blocked')
        self.assertEqual(decision({'completion_state':'blocked','pending_steps':['retry']}),'blocked')

    def test_restart_preserves_context_and_never_replays_unknown_actions(self):
        from relay.store import Relay
        home=self.scenario({'uncertain':True});task=self.start();self.wait(task,'handoff_pending')
        before=len((home/'turns.jsonl').read_text().splitlines())
        self.relay.db.execute("UPDATE tasks SET state='running' WHERE id=?",(task['id'],))
        self.relay.close();self.relay=Relay(self.root/'data')
        self.assertEqual(self.relay.task('alice',task['id'])['state'],'handoff_pending')
        self.assertFalse(self.relay.jobs);self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases'))
        self.assertEqual(len((home/'turns.jsonl').read_text().splitlines()),before)
        self.assertIn('Interrupted process',self.relay.task('alice',task['id'])['last_error'])


class AgentStopTests(unittest.TestCase):
    setUp=dashboard_fixture.LocalNativeDashboardTests.setUp
    tearDown=dashboard_fixture.LocalNativeDashboardTests.tearDown
    start=dashboard_fixture.LocalNativeDashboardTests.start
    stop=dashboard_fixture.LocalNativeDashboardTests.stop
    enroll=dashboard_fixture.LocalNativeDashboardTests.enroll
    wait=dashboard_fixture.LocalNativeDashboardTests.wait

    def test_stop_exact_agent_process_group_and_keep_other_task_running(self):
        close_native_sessions()
        for profile in ('a','b'):
            home,_=profile_paths(profile);(home/'scenario.json').write_text('{"continuous":2,"delay":60,"child":true}')
        a,b=self.enroll('a'),self.enroll('b')
        other=self.client.post('projects',{'name':'Other running project'})['id']
        self.client.post('workers/configure',{'worker_id':b,'project_ids':[self.project,other]})
        tasks=[]
        for worker,project in ((a,self.project),(b,other)):
            tasks.append(self.client.post('tasks/assign',{'worker_id':worker,'project_id':project,'prompt':PROMPT}))
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not all((profile_paths(p)[0]/'child.pid').exists() for p in ('a','b')):time.sleep(.02)
        self.assertTrue((profile_paths('a')[0]/'child.pid').exists())
        client_a=session_for('a').client;client_b=session_for('b').client
        activity=self.client.get('agents/activity')
        self.assertTrue(next(item for item in activity['agents'] if item['id']==a)['observation']['can_stop'])
        self.assertTrue(self.client.get('agents/activity?after='+activity['cursor'])['unchanged'])
        self.assertEqual(self.client.request('/api/workers/stop',{'worker_id':b,'task_id':tasks[0]['id']})[0],409)
        stopped=self.client.post('workers/stop',{'worker_id':a,'task_id':tasks[0]['id']})
        self.assertEqual(stopped['state'],'pausing')
        self.wait(tasks[0],'cancelled')
        self.assertIsNotNone(client_a.process.poll());self.assertIsNone(client_b.process.poll())
        self.assertEqual(self.client.get('tasks?project_id='+other)['tasks'][0]['state'],'running')
        self.assertIn('native_tools.py',self.server.relay.ws.inspect(self.project)['files'])
        self.assertFalse(self.server.relay.rows('SELECT * FROM workspace_leases WHERE project_id=?',(self.project,)))
        self.assertTrue(self.server.relay.rows('SELECT * FROM workspace_leases WHERE project_id=?',(other,)))
        self.assertEqual(self.client.post('workers/stop',{'worker_id':a,'task_id':tasks[0]['id']})['state'],'cancelled')
        self.client.post('workers/stop',{'worker_id':b,'task_id':tasks[1]['id']});self.wait(tasks[1],'cancelled')
        if os.name!='nt':
            for profile in ('a','b'):
                pid=int((profile_paths(profile)[0]/'child.pid').read_text())
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:os.kill(pid,0)
                    except ProcessLookupError:break
                    time.sleep(.05)
                else:self.fail('Tool child process survived Stop')
        self.assertFalse(self.server.relay.jobs)

    def test_cancel_queued_task_from_its_agent_card_only(self):
        close_native_sessions()
        for profile in ('a','b'):
            home,_=profile_paths(profile);(home/'scenario.json').write_text('{"continuous":2,"delay":60}')
        a,b=self.enroll('a'),self.enroll('b')
        running=self.client.post('tasks/assign',{'worker_id':a,'project_id':self.project,'prompt':PROMPT})
        queued=self.client.post('tasks/assign',{'worker_id':b,'project_id':self.project,'prompt':PROMPT,'queue':True})
        self.assertEqual(queued['state'],'queued')
        agents={item['id']:item['observation'] for item in self.client.get('agents/activity')['agents']}
        self.assertEqual((agents[b]['stop_action'],agents[b]['stop_task_id']),('cancel',queued['id']))
        self.assertEqual((agents[a]['stop_action'],agents[a]['stop_task_id']),('stop',running['id']))
        self.assertEqual(self.client.post('workers/stop',{'worker_id':b,'task_id':queued['id']})['state'],'cancelled')
        self.assertEqual(self.client.get('tasks?project_id='+self.project)['tasks'][1]['state'],'running')
        self.client.post('workers/stop',{'worker_id':a,'task_id':running['id']});self.wait(running,'cancelled')
        self.assertFalse(self.server.relay.jobs)


if __name__=='__main__':unittest.main()
