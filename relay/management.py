"""Explicit worker/task lifecycle. Queued tasks require a human dispatch action."""
import json
import os

from .ide import WorkspaceConflict, file_path
from .security import clean
from .store import LIVE_MODES, now, uid


class Management:
    def __init__(self, relay):
        self.relay = relay

    def remove(self, user, data):
        r = self.relay
        w = r.one('SELECT * FROM workers WHERE id=?', (data['worker_id'],))
        r.manage(user, w)
        if data.get('confirmed') is not True:raise ValueError('Removing enrollment requires confirmation')
        if r.rows("SELECT 1 FROM tasks WHERE (worker_id=? OR (worker_id IS NULL AND requested_worker_id=?)) AND state NOT IN ('completed','failed','cancelled')", (w['id'], w['id'])):
            raise WorkspaceConflict('Cancel and wait for the task checkpoint, or explicitly hand off unfinished work, before removing enrollment')
        if r.rows('SELECT 1 FROM workspace_leases WHERE worker_id=?', (w['id'],)):
            raise WorkspaceConflict('Worker still owns a workspace lease')
        r.db.execute("UPDATE workers SET removed_at=?,state='offline',opt_in=0,updated_at=? WHERE id=?", (now(), now(), w['id']))
        r._capacity(w['id'], 'offline')
        r.audit(user, 'worker_enrollment_removed', w['id'])
        return {'removed': True, 'worker_id': w['id'], 'history_preserved': True}

    def assign(self, user, data):
        from .ide import ProjectIDE
        r = self.relay
        project, worker = data['project_id'], data['worker_id']
        r.write_access(user, project)
        w = r.one('SELECT * FROM workers WHERE id=?', (worker,))
        approved, reason = r.approval(w, project, user)
        if not approved:raise PermissionError(reason)
        prompt = data.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt)>8000:raise ValueError('Task instructions must contain 1–8000 characters')
        if w['execution_mode']=='simulation' and not ('password' in prompt.lower() and 'validator' in prompt.lower()):
            raise ValueError('Simulated workers support the password validator fixture only')
        if not isinstance(data.get('queue', False), bool):raise ValueError('queue must be boolean')
        if not data.get('queue'):
            r.assert_no_human_writer(project)
            self._project_idle(project)
            eligible, reason = r.eligibility(w, project, user)
            if not eligible:raise WorkspaceConflict('Chosen agent cannot start: ' + reason + '. Explicitly queue it or resolve readiness.')
        ide = ProjectIDE(r)
        c = ide.conversation(user, project, data['conversation_id']) if data.get('conversation_id') else ide.create_conversation(user, project, prompt[:80])
        if c['archived_at']:raise ValueError('Restore the conversation before assigning work')
        if r.rows("SELECT 1 FROM tasks WHERE conversation_id=? AND state NOT IN ('completed','failed','cancelled')", (c['id'],)):
            raise WorkspaceConflict('This conversation already has an unfinished task; choose a new conversation')
        t = {'id': uid(), 'conversation_id': c['id'], 'state': 'queued', 'worker_id': None,
             'requested_worker_id': worker, 'objective': clean(prompt), 'generation': 0,
             'requested_by': user, 'execution_mode': w['execution_mode'], 'pause_after_turn': 0,
             'execution_phase': 0, 'progress_json': '{}', 'native_phases': 2}
        r.db.execute(f"INSERT INTO tasks({','.join(t)}) VALUES({','.join('?' for _ in t)})", tuple(t.values()))
        r.event(t, 'prompt', prompt, 'user', {'requested_worker_id': worker, 'user_id': user})
        r.event(t, 'queue', 'Assigned to ' + w['name'] + '; explicit Start is required while queued.', metadata={'requested_worker_id': worker})
        r.audit(user, 'task_direct_assignment', worker, project, {'task_id': t['id'], 'queued': bool(data.get('queue'))})
        return r.one('SELECT * FROM tasks WHERE id=?', (t['id'],)) if data.get('queue') else self.start(user, t['id'])

    def _project_idle(self, project, task=None):
        r = self.relay
        if r.rows("SELECT 1 FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.id!=? AND t.state NOT IN ('completed','failed','cancelled','queued')", (project, task or '')):
            raise WorkspaceConflict('Another unfinished task owns this project. Finish or cancel it first; same-project parallel execution is unsupported.')
        if r.rows('SELECT 1 FROM workspace_leases WHERE project_id=? AND task_id!=?', (project, task or '')):
            raise WorkspaceConflict('Another agent owns this workspace')

    def start(self, user, identity):
        r = self.relay
        t = r.task(user, identity)
        project = r.project(t)
        r.write_access(user, project)
        r.assert_no_human_writer(project)
        self._project_idle(project, identity)
        if t['state']!='queued':raise ValueError('Start requires a queued task')
        if r.one('SELECT archived_at FROM conversations WHERE id=?', (t['conversation_id'],))['archived_at']:
            raise ValueError('Restore this conversation before starting')
        if not t['requested_worker_id']:raise ValueError('This legacy task needs an explicit worker handoff')
        w = r._candidate(user, project, target=t['requested_worker_id'], mode=t['execution_mode'])
        r.assign(t, w, user)
        context = {}
        try:
            if t['execution_mode'] in LIVE_MODES:context = r.context(t, r.checkpoint(t, 'Before direct agent execution'))
            r._execute(t, w, False, context)
        except Exception as exc:r._execution_error(t, exc)
        return r.one('SELECT * FROM tasks WHERE id=?', (identity,))

    def tasks(self, user, project, worker=None):
        r = self.relay
        r.access(user, project)
        rows = r.rows('SELECT t.*,c.title AS conversation_title FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? ORDER BY t.rowid DESC LIMIT 200', (project,))
        return {'tasks': [t for t in rows if not worker or worker in (t['worker_id'], t['requested_worker_id'])],
                'concurrency': 'Serialized per project and per worker. Separate projects may execute concurrently; queued tasks require explicit Start.'}

    def observation(self, user, worker):
        r = self.relay
        tasks = r.rows('SELECT t.*,c.project_id,c.title AS conversation_title,p.name AS project_name FROM tasks t JOIN conversations c ON c.id=t.conversation_id JOIN projects p ON p.id=c.project_id WHERE t.worker_id=? OR t.requested_worker_id=? ORDER BY t.rowid DESC LIMIT 200', (worker, worker))
        visible = []
        for t in tasks:
            try:r.access(user, t['project_id']);visible.append(t)
            except PermissionError:pass
        current = next((t for t in visible if t['worker_id']==worker and t['state'] not in ('completed','cancelled','failed','queued')), None)
        selected = current or (visible[0] if visible else None)
        events=r.rows("SELECT event_type,content,metadata,timestamp,task_id,conversation_id,sequence FROM events WHERE task_id=? AND worker_id=? ORDER BY sequence DESC LIMIT 160",(selected['id'],worker)) if selected else []
        observable=('tool_invocation','tool_output','response','agent_delta','checkpoint','error','provider_error','execution_result','native_environment','file_change','turn_started','continuation','task_blocked','stop')
        event=next((e for e in events if e['event_type'] in observable),None)
        runs=r.rows('SELECT state,started_at,finished_at,duration_ms FROM execution_runs WHERE worker_id=? AND task_id=? ORDER BY rowid DESC LIMIT 1',(worker,selected['id'])) if selected else []
        changed_files=set();checkpoint=[]
        command=next((e for e in events if e['event_type']=='tool_invocation'),None)
        result=next((e for e in events if e['event_type']=='tool_output' and 'exit_code' in json.loads(e['metadata'])),None)
        executing=command if current and current['state']=='running' else None
        if executing:
            key=json.loads(executing['metadata'])
            for e in events:
                if e['sequence']<=executing['sequence']:break
                meta=json.loads(e['metadata'])
                if e['event_type']=='tool_output' and meta.get('provider_item_id')==key.get('provider_item_id') and meta.get('native_turn_id')==key.get('native_turn_id'):
                    executing=None;break
        if selected:
            checkpoint=r.rows('SELECT git_commit,reason,created_at FROM workspace_checkpoints WHERE task_id=? AND worker_id=? ORDER BY rowid DESC LIMIT 1',(selected['id'],worker))
            # File counts come from actual file-change events or final context,
            # never from model text that says it intends to edit something.
            for row in r.rows("SELECT metadata,content FROM events WHERE task_id=? AND worker_id=? AND event_type IN ('file_change','tool_output') ORDER BY sequence DESC LIMIT 1000",(selected['id'],worker)):
                meta=json.loads(row['metadata']);changes=meta.get('changes',[])
                if not changes:
                    try:changes=json.loads(row['content'])
                    except (ValueError,TypeError):changes=[]
                for change in changes if isinstance(changes,list) else []:
                    if isinstance(change,dict) and isinstance(change.get('path'),str):changed_files.add(change['path'])
            contexts=r.rows('SELECT data FROM contexts WHERE task_id=? ORDER BY version DESC LIMIT 1',(selected['id'],))
            if contexts:changed_files.update(json.loads(contexts[0]['data']).get('files_changed',[]))
        errors=[e for e in events if e['event_type'] in ('error','provider_error','task_blocked','capacity_blocked')][:3]
        phase='idle'
        if selected:
            state=selected['state'];progress=json.loads(selected.get('progress_json') or '{}')
            if state=='queued':phase='waiting_for_resource' if r.one('SELECT state FROM workers WHERE id=?',(worker,))['state']!='available' else 'queued'
            elif state=='pausing':phase='stopping' if selected.get('pending_action')=='cancel' else 'pausing'
            elif state=='handoff_pending':phase='blocked' if selected.get('last_error') or progress.get('completion_state') in ('blocked','uncertain','needs_authorization') else 'paused'
            elif state=='cancelled':phase='stopped'
            elif state=='running':phase='running' if events else 'starting'
            else:phase=state
        # Stop interrupts a live execution; Cancel discards a queued or paused
        # task without touching other workers. Both use the same exact-task route.
        can_stop=False;stop_action=None;stop_target=None
        queued=next((t for t in visible if worker in (t['worker_id'],t['requested_worker_id']) and t['state']=='queued'),None)
        target=current if current and current['state'] in ('running','pausing','handoff_pending') else queued
        if target:
            try:
                r.write_access(user,target['project_id'])
                if target['execution_mode']!='native' or user==r.native_actor():
                    stop_target=target['id'];stop_action='stop' if target['state'] in ('running','pausing') else 'cancel';can_stop=stop_action=='stop'
            except PermissionError:pass
        return {'current_task':current,'latest_task':selected,'last_action':event,'phase':phase,
                'latest_run':runs[0] if runs else None,'reported_changed_files':sorted(changed_files),
                'last_command':command,'executing_command':executing,'last_tool_result':result,
                'checkpoint':checkpoint[0] if checkpoint else None,'recent_errors':errors,
                'blocker':selected.get('last_error') if selected else None,'can_stop':can_stop,'stop_action':stop_action,'stop_task_id':stop_target,
                'activity':self._activity(phase,executing,event,command,result),
                'provider':'Codex' if selected and selected['execution_mode'] in LIVE_MODES else None,
                'model':os.environ.get('RELAY_NATIVE_MODEL') if selected and selected['execution_mode']=='native' else None}

    @staticmethod
    def _activity(phase,executing,event,command,result):
        # One line derived only from recorded events; never inferred intent.
        if executing:return 'Running command: '+executing['content'][:180]
        if phase not in ('running','starting','stopping','pausing'):return phase.replace('_',' ').capitalize()
        kind=event['event_type'] if event else None
        if kind=='tool_output' and result:
            code=json.loads(result['metadata']).get('exit_code')
            return 'Command finished (exit '+str(code)+')'+(': '+command['content'][:150] if command else '')
        labels={'file_change':'Applying file changes','agent_delta':'Writing response','response':'Reported progress',
                'turn_started':'Working on the objective; waiting for the next model or tool event','continuation':'Continuing after a checkpoint',
                'checkpoint':'Saved checkpoint','native_environment':'Preparing the project environment','stop':'Stopping: waiting for process cleanup',
                'provider_error':'Provider reported an error'}
        return labels.get(kind,phase.replace('_',' ').capitalize())

    def activity(self,user,after=''):
        import hashlib
        r=self.relay;r.org_member(user);items=[]
        for w in r.rows("SELECT * FROM workers WHERE organization_id='orgdemo' AND removed_at IS NULL ORDER BY id"):
            if w['execution_mode']=='native' and not r.native_enabled:continue
            owner=w['owner_type']=='user' and w['owner_id']==user
            shared=r.rows('SELECT 1 FROM worker_projects w JOIN memberships m ON m.project_id=w.project_id WHERE w.worker_id=? AND m.user_id=?',(w['id'],user))
            if owner or shared or r.is_admin(user):
                items.append({'id':w['id'],'status':w['state'],'observation':self.observation(user,w['id'])})
        cursor=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
        return {'cursor':cursor,'unchanged':cursor==after,'agents':[] if cursor==after else items}

    def stop(self,user,data):
        r=self.relay;t=r.task(user,data['task_id']);r.write_access(user,r.project(t))
        if data['worker_id'] not in (t['worker_id'],t.get('requested_worker_id')):raise WorkspaceConflict('This task belongs to a different agent; refresh its activity')
        if t['execution_mode']=='native' and user!=r.native_actor():raise PermissionError('Only the pinned local owner can control native execution')
        if t['state'] in ('completed','failed','cancelled'):return t
        if t['state']=='pausing' and t.get('pending_action')=='cancel':return t
        return r.cancel(user,t)

    def review(self, user, project, task=None, before=None, after=None):
        r = self.relay
        r.access(user, project)
        if task:
            t = r.task(user, task)
            if r.project(t)!=project:raise PermissionError('Task belongs to another project')
            checkpoints = r.rows('SELECT * FROM workspace_checkpoints WHERE project_id=? AND task_id=? ORDER BY rowid', (project, task))
            if len(checkpoints)<2:raise ValueError('This task has no completed checkpoint change set yet')
            before, after = checkpoints[0]['git_commit'], checkpoints[-1]['git_commit']
        known = {row['git_commit'] for row in r.rows('SELECT git_commit FROM workspace_checkpoints WHERE project_id=?', (project,))}
        if not before or not after or before not in known or after not in known:raise ValueError('Choose two recorded project checkpoints')
        workspace = r.ws.path(project)
        names = r.ws.git(workspace, 'diff', '--name-only', '-z', before, after, '--', raw=True).split('\0')
        allowed = []
        for name in filter(None, names):
            try:file_path(name, write=True);allowed.append(name)
            except (ValueError, PermissionError):pass
        diff = r.ws.git(workspace, '--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv', before, after, '--', *allowed) if allowed else ''
        return {'before': before, 'after': after, 'files': allowed, 'diff': clean(diff[:200000]),
                'attribution': 'Recorded task/checkpoint authorship; the diff contains the saved changes between these checkpoints. No line-level authorship is inferred.',
                'parallel_integration_supported': False}
