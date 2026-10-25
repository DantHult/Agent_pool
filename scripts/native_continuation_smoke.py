"""Opt-in real native continuation proof. Never logs or copies authentication."""
import argparse
import json
import re
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.native import close_native_sessions,native_readiness  # noqa: E402
from relay.store import Relay  # noqa: E402

OBJECTIVE=('Create normalize_text.py with normalize(text) that lowercases and collapses whitespace. '
           'Use two turns: in the first turn implement the function only, then return completion_state=continue '
           'with pending_steps to write tests and documentation. In the following turn inspect the saved checkpoint, '
           'add test_normalize_text.py with at least four unittest cases (empty, mixed case, tabs/newlines, Unicode), '
           'run python3 -m unittest discover -v, and add a README usage example. Complete only after tests pass. '
           'Stay in this project. Do not download packages or access credentials.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true');parser.add_argument('--profile',choices=('a','b','c'),default='c')
    parser.add_argument('--data',type=Path,default=Path('native-continuation-proof'))
    args=parser.parse_args()
    if not args.run_live:print('SKIP: use --run-live to authorize real Codex inference');return
    if args.data.exists():raise ValueError('Use a fresh proof directory; existing evidence is never overwritten')
    args.data.mkdir(mode=0o700,parents=True)
    report={'verified':False,'provider_execution':'not started','profile':args.profile,'objective':OBJECTIVE}
    relay=None
    try:
        checks,details=native_readiness((args.profile,))
        report['preflight']=[{'check':name,'passed':ok} for name,ok in checks]
        if not all(ok for _,ok in checks):raise RuntimeError('Preflight blocked: '+'; '.join(details))
        relay=Relay(args.data/'data',native_enabled=True)
        project=relay.mutate('alice','projects',{'name':'Real same-profile continuation proof'})['id']
        conversation=relay.detail('alice',project,light=True)['conversation']['id']
        worker=relay.mutate('alice','workers/enroll',{'display_name':'Native '+args.profile.upper(),'owner_type':'user','execution_mode':'native','native_profile':args.profile,'project_ids':[project]})
        task=relay.mutate('alice','prompt',{'conversation_id':conversation,'worker_id':worker['id'],'execution_mode':'native','prompt':OBJECTIVE})
        report.update({'provider_execution':'requested','task_id':task['id'],'project_id':project})
        deadline=time.monotonic()+900
        while time.monotonic()<deadline:
            with relay.lock:
                current=relay.task('alice',task['id'])
                if current['id'] not in relay.jobs:break
            time.sleep(.25)
        else:
            with relay.lock:relay.cancel('alice',current)
            raise RuntimeError('Proof deadline exceeded; cancellation requested')
        with relay.lock:
            events=relay.rows('SELECT * FROM events WHERE task_id=? ORDER BY sequence',(task['id'],))
            checkpoints=relay.rows('SELECT git_commit,reason FROM workspace_checkpoints WHERE task_id=?',(task['id'],))
            turns=[json.loads(e['metadata']) for e in events if e['event_type']=='turn_started']
            report.update({'state':current['state'],'turns':turns,'checkpoints':checkpoints,'assignments':len(relay.rows('SELECT * FROM assignments WHERE task_id=?',(task['id'],))),
                           'test_output':[e['content'] for e in events if e['event_type']=='tool_output'],
                           'result':json.loads(current['progress_json'])})
            assert current['state']=='completed',current.get('last_error')
            assert len(turns)>=2,'Provider did not continue beyond an intermediate turn'
            assert any(c['reason']=='Intermediate native progress' for c in checkpoints)
            assert report['assignments']==1
            assert len({t['native_thread_id'] for t in turns})==1
            # The objective asks for at least four cases; accept any larger passing run.
            assert any(int(m.group(1))>=4 and 'OK' in output for output in report['test_output'] for m in [re.search(r'Ran (\d+) tests?',output)] if m),'Expected actual successful tool output with at least four tests'
            assert not relay.rows('SELECT 1 FROM workspace_leases WHERE project_id=?',(project,))
            report['verified']=True;report['provider_execution']='actual authenticated native Codex, multi-turn verified'
    except Exception as exc:
        report['blocker']=str(exc)
    finally:
        if relay:relay.close()
        close_native_sessions()
        (args.data/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    if not report['verified']:raise SystemExit(1)


if __name__=='__main__':main()
