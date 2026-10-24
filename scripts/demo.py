"""Repeatable, credential-free Relay handoff demonstration.

Run: python3 scripts/demo.py [--data /new/persistent/directory]
Without --data the demonstration uses a temporary workspace and cleans it up.
"""
import argparse
from contextlib import contextmanager
import fcntl
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.store import Relay  # noqa: E402

PROMPT = 'Add a password-strength validator and tests to this sample project.'


@contextmanager
def exclusive_demo_directory(data):
    directory = Path(data)
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / 'server.lock', 'a+')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                'This Relay data directory is in use. Stop the existing server/demo, '
                'or choose a new --data directory. Do not delete server.lock.'
            ) from None
        yield
    finally:
        lock.close()


def run(data):
    # Acquire the same filesystem lock as the HTTP server before Relay startup
    # recovery can inspect or change assignments/worktree leases.
    with exclusive_demo_directory(data):
        _run_owned(data)


def _run_owned(data):
    relay = Relay(data)
    try:
        before = relay.detail('alice', 'demo')
        conversation = before['conversation']['id']
        active = [t for t in before['tasks'] if t['state'] not in ('completed', 'failed', 'cancelled')]
        if active:
            raise SystemExit('This data directory has an active task; use a new --data directory for a repeatable demo.')
        for owner, worker in [('alice', 'codexA'), ('bob', 'codexB')]:
            relay.mutate(owner, 'workers/configure', {
                'worker_id': worker, 'opt_in': True, 'state': 'available',
                'project_ids': ['demo'] + (['private'] if owner == 'bob' else []),
            })
        task = relay.mutate('alice', 'prompt', {
            'conversation_id': conversation, 'worker_id': 'codexA', 'prompt': PROMPT,
        })
        assert task['worker_id'] == 'codexA', task
        print(f'1. Relay conversation: {conversation}')
        print('2. Alex authorized separately owned simulated Worker A. validator.py exists in the server-side Git workspace.')
        first_events = relay.events('alice', conversation, limit=100)['events']
        result = relay.mutate('alice', 'limit', {
            'task_id': task['id'], 'generation': task['generation'],
        })
        assert result['worker_id'] == 'codexB' and result['state'] == 'completed', result
        assert result['conversation_id'] == conversation, result
        detail = relay.detail('alice', 'demo')
        handoff = detail['handoffs'][-1]
        events = []
        cursor = 0
        while True:
            page = relay.events('alice', conversation, after=cursor, limit=100)
            events.extend(page['events'])
            if len(page['events']) < 100:
                break
            cursor = page['next_after']
        assert events[:len(first_events)] == first_events
        assert '.relay/HANDOFF.md' in detail['workspace']['files']
        outputs = [e['content'] for e in events if e['worker_id'] == 'codexB' and e['event_type'] == 'tool_output']
        assert any('Ran 5 tests' in output and 'OK' in output for output in outputs), outputs
        print('3. Simulated quota event safely checkpointed Worker A and assigned approved Worker B.')
        print(f"   Git checkpoint: {handoff['checkpoint']}")
        print(f"   Context version: {handoff['context_version']}")
        print('4. Worker B loaded the unfinished objective, code, test information, and conversation reference.')
        print('5. Worker B completed the validator suite: five actual unittest checks passed.')
        print(f'6. Same Relay conversation, {len(events)} durable events, both worker identities retained.')
        print(f'   Workspace: {Path(data).resolve() / "workspaces" / "demo"}')
        print('   Capacity is simulated; no API calls, personal subscriptions, or credentials are used.')
        relay.close()
        relay = None
        relay = Relay(data)
        restored = relay.detail('alice', 'demo')
        assert restored['conversation']['id'] == conversation
        assert restored['contexts'] == detail['contexts']
        assert restored['assignments'] == detail['assignments']
        assert restored['checkpoints'] == detail['checkpoints']
        print('7. Backend reopened: conversation, context snapshots, Git checkpoints, and assignments survived.')
        print('PASS: end-to-end owned-worker handoff demo')
    finally:
        if relay is not None:
            relay.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', help='Optional persistent demo data directory (use a new directory).')
    args = parser.parse_args()
    if args.data:
        run(args.data)
    else:
        with tempfile.TemporaryDirectory(prefix='relay-demo-') as directory:
            run(directory)


if __name__ == '__main__':
    main()
