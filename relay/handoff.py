"""Portable HANDOFF.md for continuing a conversation's work with any agent.

Unlike .relay/HANDOFF.md (Relay's internal context for its own workers), this
is readable Markdown with no Relay endpoints, written for an agent outside Relay.
A project's HANDOFF.md combines all of its active conversations.
"""
import json
import re
from datetime import datetime, timezone

from .security import clean


def _private_clone_paths():
    from .native import native_root
    # Agents cite files inside their temporary private clones; those paths
    # are meaningless elsewhere, so cite them relative to the project.
    return re.compile(re.escape(str(native_root() / 'workspaces')) + r'/[abc]/[0-9a-f]{32}/?')


def _fenced(text):
    fence = '````' if '```' in text else '```'
    return fence + 'text\n' + text.rstrip() + '\n' + fence


def _bullets(items):
    def bullet(item):
        # Continuation lines stay inside their bullet.
        first, *rest = [line.rstrip() for line in item.strip().splitlines()]
        return '\n'.join(['- ' + first] + ['  ' + line if line else '' for line in rest])
    return '\n'.join(bullet(item) for item in items if isinstance(item, str) and item.strip())


def _work(relay, conversation):
    """Everything recorded about one conversation's agent work."""
    tasks = relay.rows('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid', (conversation,))
    latest = tasks[-1] if tasks else None
    contexts = relay.rows('SELECT data FROM contexts WHERE task_id=? ORDER BY version DESC LIMIT 1', (latest['id'],)) if latest else []
    return {'tasks': tasks, 'latest': latest,
            'progress': json.loads(latest.get('progress_json') or '{}') if latest else {},
            'context': json.loads(contexts[0]['data']) if contexts else {},
            'agents': [row['name'] for row in relay.rows('SELECT DISTINCT w.name FROM assignments a JOIN tasks t ON t.id=a.task_id JOIN workers w ON w.id=a.worker_id WHERE t.conversation_id=? ORDER BY a.created_at', (conversation,))],
            'handoffs': relay.rows('SELECT h.timestamp,h.reason,h.checkpoint,s.name AS source,d.name AS target FROM handoffs h JOIN tasks t ON t.id=h.task_id LEFT JOIN workers s ON s.id=h.source LEFT JOIN workers d ON d.id=h.target WHERE t.conversation_id=? ORDER BY h.timestamp', (conversation,)),
            'response': relay.rows("SELECT content FROM events WHERE conversation_id=? AND event_type='response' ORDER BY sequence DESC LIMIT 1", (conversation,))}


def _status(work):
    latest, progress = work['latest'], work['progress']
    return latest['state'].replace('_', ' ') + (' · agent reported: ' + progress['completion_state'] if progress.get('completion_state') else '')


def _sections(work, level):
    """Requests, results and history of one conversation, with headings at `level`."""
    heading = '#' * level + ' '
    tasks, latest, progress, context = work['tasks'], work['latest'], work['progress'], work['context']
    lines = []
    if tasks:
        lines += ['', heading + 'Requests in this conversation', '']
        for index, t in enumerate(tasks, 1):
            lines.append(str(index) + '. ' + t['objective'].strip().replace('\n', '\n   '))
            lines.append('   _' + t['state'].replace('_', ' ').capitalize() + (' · latest request; the sections below describe it_' if t is latest else '_'))
    summary = progress.get('summary') or (work['response'][0]['content'][:4000] if work['response'] else '')
    if summary:
        lines += ['', heading + 'Latest summary', '', summary.strip()]
    remaining = progress.get('pending_steps') or ([] if latest and latest['state'] == 'completed' else context.get('todos', []))
    for title, items in (('Completed work', progress.get('completed_work') or context.get('completed_work')), ('Remaining work', remaining),
                         ('Decisions', progress.get('decisions')), ('Files changed', ['`' + name + '`' for name in context.get('files_changed', [])]),
                         ('Test results', progress.get('test_results') or context.get('test_results')), ('Acceptance evidence', progress.get('acceptance_evidence')),
                         ('Known limitations', progress.get('limitations')), ('Errors', progress.get('errors') or context.get('errors'))):
        if items:
            lines += ['', heading + title, '', _bullets(items)]
    if not (progress.get('test_results') or context.get('test_results')) and context.get('tests') and context['tests'] != 'Tests not run yet':
        lines += ['', heading + 'Last command output', '', _fenced(context['tests'][-3000:])]
    return lines


def _history(work, level):
    history = [(h['source'] or 'Unassigned') + ' handed off to ' + (h['target'] or 'the next agent') + ' (' + (h['reason'] or 'no reason recorded') + ')'
               + (' at `' + h['checkpoint'][:7] + '`' if h['checkpoint'] else '') for h in work['handoffs']]
    return ['', '#' * level + ' Agent history', '', _bullets(history)] if history else []


def _project_facts(relay, user, project, checkpoint):
    """Project, folder (local owner only), branch, and uncommitted source changes."""
    workspace = relay.ws.path(project['id'])
    lines = ['- **Project:** ' + project['name']]
    # Only the local owner's own machine holds these files at this path.
    if relay.local_owner_id == user:
        lines.append('- **Project files:** `' + str(workspace) + '` (a Git repository)')
    lines.append('- **Branch:** `' + relay.ws.git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD') + '` at `' + relay.ws.git(workspace, 'rev-parse', '--short', 'HEAD') + '`'
                 + (' (last Relay checkpoint: ' + checkpoint[0]['reason'] + ')' if checkpoint else ''))
    # Relay's own metadata is not the project's work.
    uncommitted = '\n'.join(line for line in relay.ws.status(project['id']).splitlines() if not line.split(maxsplit=1)[-1].startswith('.relay'))
    return lines, uncommitted


def _intro(agents, nobody):
    return ('> Exported from Relay on ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') + '. '
            + ('Work so far by ' + ', '.join(agents) + '. ' if agents else nobody + ' ')
            + 'This is a summary; check the files before relying on it.')


def _finish(relay, user, project, lines, metadata):
    text = _private_clone_paths().sub('', '\n'.join(lines) + '\n')
    relay.audit(user, 'handoff_exported', project=project, metadata=metadata)
    return {'filename': 'HANDOFF.md', 'content': clean(text)}


def export_handoff(relay, user, conversation):
    c = relay.one('SELECT * FROM conversations WHERE id=?', (conversation,))
    project = relay.access(user, c['project_id'])
    work = _work(relay, conversation)
    checkpoint = relay.rows('SELECT git_commit,reason FROM workspace_checkpoints WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (conversation,))
    facts, uncommitted = _project_facts(relay, user, project, checkpoint)
    lines = ['# Handoff: ' + c['title'], '', _intro(work['agents'], 'No agent has worked in this conversation yet.'), '', *facts]
    if work['latest']:
        lines.append('- **Status:** ' + _status(work))
    lines += ['', '## How to continue', '',
              '1. Work in the project files' + (' above' if relay.local_owner_id == user else '') + '. Run `git status` and `git log` first; uncommitted changes are listed below.',
              '2. Read the latest request and the remaining work, then confirm the current state by running the tests.',
              '3. Stay within what was requested. Do not commit secrets or credentials.']
    lines += _sections(work, 2)
    if uncommitted:
        lines += ['', '## Uncommitted changes', '', _fenced(uncommitted)]
    lines += _history(work, 2)
    return _finish(relay, user, c['project_id'], lines, {'conversation_id': conversation})


def export_project_handoff(relay, user, project_id):
    """One HANDOFF.md for a whole project: each active conversation in turn."""
    project = relay.access(user, project_id)
    conversations = relay.rows('SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC,rowid DESC', (project_id,))
    active = [c for c in conversations if not c.get('archived_at')]
    works = {c['id']: _work(relay, c['id']) for c in active}
    agents = list(dict.fromkeys(name for work in works.values() for name in work['agents']))
    checkpoint = relay.rows('SELECT git_commit,reason FROM workspace_checkpoints WHERE project_id=? ORDER BY rowid DESC LIMIT 1', (project_id,))
    facts, uncommitted = _project_facts(relay, user, project, checkpoint)
    archived = len(conversations) - len(active)
    lines = ['# Handoff: ' + project['name'], '', _intro(agents, 'No agent has worked in this project yet.'), '', *facts,
             '- **Conversations:** ' + str(len(active)) + (' (' + str(archived) + ' archived, not included)' if archived else ''),
             '', '## How to continue', '',
             '1. Work in the project files' + (' above' if relay.local_owner_id == user else '') + '. Run `git status` and `git log` first; uncommitted changes are listed below.',
             '2. Each conversation below is a separate line of work on the same files, most recently active first. Read the one you are continuing, then confirm the current state by running the tests.',
             '3. Stay within what was requested. Do not commit secrets or credentials.']
    if uncommitted:
        lines += ['', '## Uncommitted changes', '', _fenced(uncommitted)]
    for c in active:
        work = works[c['id']]
        lines += ['', '## Conversation: ' + c['title'], '',
                  '_' + (_status(work).capitalize() + (' · worked on by ' + ', '.join(work['agents']) if work['agents'] else '') if work['latest'] else 'No agent work yet')
                  + (' · last active ' + c['updated_at'][:10] if c.get('updated_at') else '') + '_']
        lines += _sections(work, 3) + _history(work, 3)
    return _finish(relay, user, project_id, lines, {'scope': 'project'})
