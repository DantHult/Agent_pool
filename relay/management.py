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
        event = None
        for t in visible:
            candidates = r.rows("SELECT event_type,content,metadata,timestamp,task_id,conversation_id FROM events WHERE task_id=? AND worker_id=? AND event_type IN ('tool_invocation','tool_output','response','agent_delta','checkpoint','error','execution_result','native_environment') ORDER BY sequence DESC LIMIT 1", (t['id'], worker))
            if candidates and (not event or candidates[0]['timestamp']>event['timestamp']):event = candidates[0]
        runs = r.rows('SELECT state,started_at,finished_at,duration_ms FROM execution_runs WHERE worker_id=? ORDER BY rowid DESC LIMIT 1', (worker,)) if selected else []
        # Do not expose runs from inaccessible projects.
        if runs and selected:
            runs = r.rows('SELECT state,started_at,finished_at,duration_ms FROM execution_runs WHERE worker_id=? AND task_id=? ORDER BY rowid DESC LIMIT 1', (worker, selected['id']))
        changed_files = set()
        last_command, checkpoint = [], []
        if selected:
            last_command = r.rows("SELECT content,timestamp FROM events WHERE task_id=? AND worker_id=? AND event_type='tool_invocation' ORDER BY sequence DESC LIMIT 1", (selected['id'], worker))
            checkpoint = r.rows('SELECT git_commit,reason,created_at FROM workspace_checkpoints WHERE task_id=? AND worker_id=? ORDER BY rowid DESC LIMIT 1', (selected['id'], worker))
            for row in r.rows("SELECT metadata,content FROM events WHERE task_id=? AND worker_id=? AND event_type IN ('tool_invocation','tool_output','native_patch')", (selected['id'], worker)):
                metadata = json.loads(row['metadata'])
                changes = metadata.get('changes', [])
                if not changes:
                    try:changes = json.loads(row['content'])
                    except (ValueError, TypeError):changes = []
                for change in changes if isinstance(changes, list) else []:
                    if isinstance(change, dict) and isinstance(change.get('path'), str):changed_files.add(change['path'])
        return {'current_task': current, 'latest_task': selected, 'last_action': event,
                'phase': current['state'] if current else 'idle', 'latest_run': runs[0] if runs else None,
                'reported_changed_files': sorted(changed_files),
                'last_command': last_command[0] if last_command else None, 'checkpoint': checkpoint[0] if checkpoint else None,
                'provider': 'Codex' if selected and selected['execution_mode'] in LIVE_MODES else None,
                'model': os.environ.get('RELAY_NATIVE_MODEL') if selected and selected['execution_mode']=='native' else None}

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
