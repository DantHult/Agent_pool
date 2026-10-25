"""Durable Relay orchestration with explicit grants, capacity and one writer lease."""
import json
import hashlib
import os
import time
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .migrations import migrate
from .security import clean, scrub, reject_credentials
from .workspace import Workspace
from .workers import get_adapter, UnsupportedExecutionMode, PERSONAL_UNSUPPORTED_REASON

ACTIVE=('queued','running','pausing','checkpointed','handoff_pending')
TERMINAL=('completed','failed','cancelled')
LIVE_MODES=('organization_api','native')

def uid(): return uuid.uuid4().hex
def now(): return datetime.now(timezone.utc).isoformat()

class Relay:
    def __init__(self,root,demo=True,native_enabled=None,local_owner_id=None):
        self.demo=demo
        self.native_enabled=(demo and os.environ.get('RELAY_ENABLE_NATIVE')=='1') if native_enabled is None else native_enabled
        self.local_owner_id=local_owner_id
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        if not demo:
            uid=getattr(os,'getuid',lambda:None)()
            if self.root.is_symlink() or (uid is not None and self.root.stat().st_uid!=uid):raise ValueError('Shared data directory must be owned by this OS user and cannot be a symlink')
            self.root.chmod(0o700)
        self.lock=threading.RLock()
        self.started=time.time()
        self.db=sqlite3.connect(self.root/'relay.sqlite',check_same_thread=False,isolation_level=None,timeout=15)
        self.db.row_factory=sqlite3.Row
        self.jobs={}
        self.unsafe_tasks=set()
        self.closing=False
        self.health_cache={}
        self.health_jobs={}
        try:
            migrate(self.db)
            if not demo:
                for name in ('relay.sqlite','relay.sqlite-wal','relay.sqlite-shm'):
                    path=self.root/name
                    if path.exists():path.chmod(0o600)
            self.ws=Workspace(self.root/'workspaces')
            from .management import Management
            self.management=Management(self)
            if demo:self._seed()
            else:self.db.execute("UPDATE organizations SET is_demo=0,name=CASE WHEN name='Relay Labs (demo)' THEN 'Relay organization' ELSE name END WHERE id='orgdemo'")
            self._recover()
            self._restore_relay_paused_workers()
        except Exception:
            # Windows cannot delete a TemporaryDirectory while SQLite still holds the file.
            try:self.db.close()
            except sqlite3.Error:pass
            raise

    def rows(self,sql,args=()): return [dict(r) for r in self.db.execute(sql,args).fetchall()]
    def one(self,sql,args=()):
        r=self.db.execute(sql,args).fetchone()
        if r is None: raise ValueError('Not found')
        return dict(r)
    def adapter(self,worker):
        if worker.get('execution_mode')=='native':
            worker={**worker,'_native_owner_id':self.native_actor(),'_native_enabled':self.native_enabled}
        return get_adapter(worker)
    def native_actor(self):
        from .native import native_owner
        return self.local_owner_id or native_owner()
    def native_profiles(self,user,project=None,cached=False):
        if not self.local_owner_id or not self.native_enabled or user!=self.local_owner_id:raise PermissionError('Native profiles require the pinned local-owner session')
        if project:self.access(user,project)
        from .native import PROFILE_IDS
        profiles=[]
        for profile in PROFILE_IDS:
            enrolled=self.rows("SELECT * FROM workers WHERE execution_mode='native' AND native_profile=? AND removed_at IS NULL",(profile,))
            w=enrolled[0] if enrolled else {'id':'profile-'+profile,'owner_type':'user','owner_id':user,'execution_mode':'native','native_profile':profile,'state':'available','opt_in':True}
            if w['owner_id']!=user:
                profiles.append({'profile':profile,'status':'unavailable','available':False,'reason':'This profile belongs to a different worker owner; use a dedicated local-owner data directory'})
                continue
            health=self.worker_health(w,cached);status=health.get('status','unavailable')
            if enrolled:w=self._quota_recovered(w,health)
            if w['state']=='quota_blocked':status='quota_blocked'
            elif w['state']=='busy':status='busy'
            elif w['state'] in ('paused','offline') or not w['opt_in']:status='paused' if w['state']!='offline' else 'unavailable'
            profiles.append({'profile':profile,'status':status,'available':status=='available','authentication':health.get('authentication','unknown'),'worker_id':w['id'] if enrolled else None,'owner_name':self.user(user)['name'],'reason':health.get('reason','Unavailable'),'remaining_quota':None,'usage':health.get('usage'),'assigned_projects':self.worker_assignments(user,w['id'])})
        return {'profiles':profiles,'execution_mode':'native','owner_id':user,'scope':'authenticated local owner; loopback only'}
    def enroll_native_profile(self,user,profile,project):
        self.write_access(user,project)
        status=next((p for p in self.native_profiles(user,project)['profiles'] if p['profile']==profile),None)
        if not status:raise ValueError('Unknown native profile; choose a, b or c')
        if not status['available']:raise ValueError('Native profile '+profile+' is '+status['status']+'. '+status['reason'])
        if status['worker_id']:
            worker=status['worker_id'];grants=[r['project_id'] for r in self.rows('SELECT project_id FROM worker_projects WHERE worker_id=?',(worker,))]
            return self.mutate(user,'workers/configure',{'worker_id':worker,'project_ids':list(set(grants+[project]))})
        return self.mutate(user,'workers/enroll',{'display_name':'Native '+profile.upper(),'owner_type':'user','execution_mode':'native','native_profile':profile,'project_ids':[project]})
    def user(self,user): return self.one('SELECT * FROM users WHERE id=?',(user,))
    def org_member(self,user,org='orgdemo'):
        self.user(user)
        memberships=self.rows('SELECT * FROM organization_memberships WHERE organization_id=? AND user_id=?',(org,user))
        if not memberships: raise PermissionError('Organization membership required')
        return memberships[0]
    def is_admin(self,user,org='orgdemo'):
        return bool(self.rows("SELECT 1 FROM organization_memberships WHERE organization_id=? AND user_id=? AND role='admin'",(org,user)))
    def access(self,user,project):
        p=self.one('SELECT * FROM projects WHERE id=?',(project,))
        self.org_member(user,p['organization_id'])
        if not self.is_admin(user,p['organization_id']) and not self.rows('SELECT 1 FROM memberships WHERE user_id=? AND project_id=?',(user,project)):
            raise PermissionError('Project membership required')
        return p
    def write_access(self,user,project):
        p=self.access(user,project);organization=self.org_member(user,p['organization_id'])
        if organization['role']=='viewer':raise PermissionError('Viewer accounts cannot change projects or execute workers')
        if not self.is_admin(user,p['organization_id']):
            role=self.one('SELECT role FROM memberships WHERE user_id=? AND project_id=?',(user,project))['role']
            if role not in ('admin','member','engineer'):raise PermissionError('Project is read-only for this account')
        return p
    def authorize_mutation(self,user,method,data):
        if self.org_member(user)['role']=='viewer':raise PermissionError('Viewer accounts are read-only')
        project=data.get('project_id')
        if data.get('conversation_id'):project=self.one('SELECT project_id FROM conversations WHERE id=?',(data['conversation_id'],))['project_id']
        if data.get('task_id'):project=self.project(self.task(user,data['task_id']))
        if project:self.write_access(user,project)
    def set_membership(self,user,data):
        project=data['project_id'];self.write_access(user,project)
        if not self.is_admin(user) and self.one('SELECT role FROM memberships WHERE user_id=? AND project_id=?',(user,project))['role']!='admin':raise PermissionError('Project administrator required')
        target=data['user_id'];self.org_member(target);role=data.get('role','member')
        if role not in ('admin','member','viewer','remove'):raise ValueError('Unknown project role')
        if target==user and not self.is_admin(user):raise ValueError('Ask another administrator to change your project role')
        if role=='remove':self.db.execute('DELETE FROM memberships WHERE user_id=? AND project_id=?',(target,project))
        else:self.db.execute('INSERT OR REPLACE INTO memberships VALUES(?,?,?)',(target,project,role))
        self.audit(user,'membership_changed',project=project,metadata={'member':target,'role':role})
        return {'ok':True}
    def _seed(self):
        for row in [('alice','Alex Morgan','engineer'),('bob','Sam Rivera','engineer'),('admin','Jordan Lee','admin')]:
            self.db.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',row)
            self.db.execute('INSERT OR IGNORE INTO organization_memberships VALUES(?,?,?)',('orgdemo',row[0],'admin' if row[2]=='admin' else 'member'))
        seeded=not self.rows('SELECT 1 FROM projects')
        if seeded:
            self.create_project('alice','Password toolkit','demo')
            self.db.execute("INSERT INTO memberships VALUES('bob','demo','engineer')")
            self.create_project('bob','Private sandbox','private')
        for worker,name,owner in [('codexA','Codex Worker A','alice'),('codexB','Codex Worker B','bob')]:
            if not self.rows('SELECT 1 FROM workers WHERE id=?',(worker,)):
                self._insert_worker(worker,name,'user',owner,'simulation',True,1,None)
                self._grant(owner,worker,'demo')
                if owner=='bob': self._grant(owner,worker,'private')
    def _restore_relay_paused_workers(self):
        """Earlier releases paused idle workers after handoffs, errors and restarts.

        Only an owner's own pause (recorded in the audit log) or opt-out keeps
        a worker paused; anything else was Relay's and is undone once.
        """
        for w in self.rows("SELECT * FROM workers WHERE state='paused' AND opt_in=1 AND removed_at IS NULL"):
            if self.rows("SELECT 1 FROM assignments WHERE worker_id=? AND status='active'",(w['id'],)):continue
            owner=self.rows("SELECT json_extract(metadata,'$.availability') AS state FROM audit_events WHERE worker_id=? AND action='worker_configured' AND json_extract(metadata,'$.availability') IS NOT NULL ORDER BY rowid DESC LIMIT 1",(w['id'],))
            if owner and owner[0]['state']=='paused':continue
            self._capacity(w['id'],'available')
            self.audit(w['owner_id'],'worker_unpaused',w['id'],metadata={'reason':'paused by Relay, not by its owner'})
    def _recover(self):
        # A crashed operation may have changed Git without its final SQL update.
        # Never replay unknown or non-idempotent model/tool actions automatically.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for t in self.rows("SELECT * FROM tasks WHERE state IN ('running','pausing','checkpointed')"):
                if t['execution_mode'] in LIVE_MODES and t['worker_id']:
                    adapter=self.adapter(self.one('SELECT * FROM workers WHERE id=?',(t['worker_id'],)))
                    if not adapter.recover(t['id']):
                        self.unsafe_tasks.add(t['id'])
                        self.db.execute("UPDATE tasks SET state='pausing',last_error=? WHERE id=?",('Container cleanup is unverified. Stop the task container and restore Docker before restarting Relay.',t['id']))
                        self.event(t,'recovery','Writer lease retained: container shutdown could not be verified. Restore Docker and restart Relay before resuming.')
                        continue
                self.db.execute("UPDATE tasks SET state='handoff_pending',pending_action=NULL,last_error=? WHERE id=?",('Interrupted process; inspect saved files before retry',t['id']))
                self.db.execute("UPDATE assignments SET status='interrupted',finished_at=? WHERE task_id=? AND status='active'",(now(),t['id']))
                self.db.execute("UPDATE execution_runs SET state='interrupted',finished_at=?,summary='Backend interrupted; no coding operation was replayed' WHERE task_id=? AND state='running'",(now(),t['id']))
                # Cleanup was verified, so the idle worker may take new work; the
                # interrupted task itself still waits for an explicit resume.
                if t['worker_id']: self._release(t,status='interrupted')
                self.event(t,'recovery','Execution interrupted. Worktree retained; inspect files and use Switch Worker to resume. No tool action was replayed.')
            for lease in self.rows('SELECT * FROM workspace_leases'):
                if lease['task_id'] not in self.unsafe_tasks:
                    self.db.execute('DELETE FROM workspace_leases WHERE task_id=?',(lease['task_id'],))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK'); raise
    def create_project(self,user,name,project=None):
        if self.org_member(user)['role']=='viewer':raise PermissionError('Viewer accounts cannot create projects')
        if not isinstance(name,str) or not name.strip() or len(name)>100: raise ValueError('Project name must contain 1–100 characters')
        project=project or uid()
        self.ws.path(project)
        self.db.execute('INSERT INTO projects VALUES(?,?,?)',(project,clean(name),'orgdemo'))
        self.db.execute('INSERT INTO memberships VALUES(?,?,?)',(user,project,'engineer' if self.demo else 'admin'))
        self.db.execute('INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)',(uid(),project,clean(name),now(),now()))
        self.audit(user,'project_created',project=project)
        return {'id':project}
    def project(self,t): return self.one('SELECT project_id FROM conversations WHERE id=?',(t['conversation_id'],))['project_id']
    def task(self,user,task_id):
        t=self.one('SELECT * FROM tasks WHERE id=?',(task_id,)); self.access(user,self.project(t)); return t
    def audit(self,user,action,worker=None,project=None,metadata=None):
        self.db.execute('INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)',(uid(),'orgdemo',user,action,worker,project,now(),json.dumps(scrub(metadata or {}))))
    def event(self,t,kind,content,role='system',metadata=None,worker=None):
        content=clean(content); metadata=scrub(metadata or {})
        metadata['execution_mode']=t.get('execution_mode','simulation')
        if isinstance(t.get('generation'),int):metadata['generation']=t['generation']
        metadata.setdefault('origin','human' if role=='user' else 'relay')
        if len(content)>12000:
            artifact=uid(); (self.root/'artifacts').mkdir(exist_ok=True)
            (self.root/'artifacts'/artifact).write_text(content[:1000000])
            metadata['artifact_id']=artifact; metadata['artifact_truncated']=len(content)>1000000
            content=content[:12000]+'\n[truncated; see artifact]'
        seq=self.db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE conversation_id=?',(t['conversation_id'],)).fetchone()[0]
        project=self.project(t); org=self.one('SELECT organization_id FROM projects WHERE id=?',(project,))['organization_id']
        self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),org,project,t['conversation_id'],t['id'],seq,now(),worker or t.get('worker_id'),kind,role,content,json.dumps(metadata)))
        self.db.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now(),t['conversation_id']))

    def assert_no_human_writer(self,project):
        from .ide import WorkspaceConflict
        # A stale reservation still blocks: only its owner may recover or release it.
        if self.rows('SELECT 1 FROM human_workspace_locks WHERE project_id=?',(project,)):
            raise WorkspaceConflict('A human holds the project edit lock. Save drafts, checkpoint and release it before starting or handing off an agent.')
        commands=getattr(self,'human_commands',None)
        if commands and commands(project):
            raise WorkspaceConflict('A human command session is open for this project. Stop or close it before starting or handing off an agent.')
    def state(self,t,state):
        self.db.execute('UPDATE tasks SET state=? WHERE id=?',(state,t['id'])); t['state']=state
        self.event(t,'state',state)

    # Worker authorization and capacity are independent of project membership.
    def _insert_worker(self,worker,name,owner_type,owner,mode,opt_in,parallel,credential):
        timestamp=now(); unsupported=mode=='personal_supported'
        self.db.execute('INSERT INTO workers(id,name,state,owner_id,organization_id,owner_type,backend,execution_mode,opt_in,max_parallel_tasks,created_at,updated_at,credential_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (worker,clean(name),'unsupported' if unsupported else 'available',owner,'orgdemo',owner_type,'codex',mode,int(opt_in),parallel,timestamp,timestamp,credential))
        scope='organization:orgdemo' if mode=='organization_api' else ('unsupported:'+worker if unsupported else 'simulation:'+worker)
        self.db.execute('INSERT INTO capacity_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(worker,scope,owner,'unsupported' if unsupported else 'available','unknown' if mode=='organization_api' else mode,None,timestamp,'simulated' if mode=='simulation' else 'manual',None,'unknown'))
    def manage(self,user,w):
        self.org_member(user,w['organization_id'])
        if not self.is_admin(user,w['organization_id']) and not (w['owner_type']=='user' and w['owner_id']==user):
            raise PermissionError('Only the worker owner or organization administrator can change authorization')
    def _grant(self,user,worker,project):
        w=self.one('SELECT * FROM workers WHERE id=?',(worker,)); self.manage(user,w)
        p=self.write_access(user,project)
        if p['organization_id']!=w['organization_id']: raise PermissionError('Worker/project organization mismatch')
        self.db.execute('INSERT OR IGNORE INTO worker_projects VALUES(?,?,?,?)',(worker,project,user,now()))
    def _capacity(self,worker,state,source=None,limit_kind=None):
        w=self.one('SELECT * FROM workers WHERE id=?',(worker,)); source=source or ('simulated' if w['execution_mode']=='simulation' else 'manual')
        self.db.execute('UPDATE workers SET state=?,updated_at=? WHERE id=?',(state,now(),worker))
        self.db.execute('UPDATE capacity_sources SET availability=?,source=?,observed_at=?,limit_kind=COALESCE(?,limit_kind) WHERE worker_id=?',(state,source,now(),limit_kind,worker))
    def approval(self,w,project,user=None):
        if w.get('removed_at'): return False,'Worker enrollment was removed'
        if w['execution_mode']=='personal_supported': return False,PERSONAL_UNSUPPORTED_REASON
        if w['execution_mode']=='native':
            if not self.native_enabled:return False,'Native execution requires the authenticated local-owner dashboard; demo/shared execution is disabled'
            if w['owner_type']!='user' or w['owner_id']!=self.native_actor() or user!=w['owner_id']:
                return False,'Native subscription execution must be initiated by its configured local owner; cross-user delegation is disabled'
        if not w['opt_in']: return False,'Owner has not opted in'
        if not self.rows('SELECT 1 FROM worker_projects WHERE worker_id=? AND project_id=?',(w['id'],project)): return False,'Owner/admin approval required for this project'
        p=self.one('SELECT * FROM projects WHERE id=?',(project,))
        if p['organization_id']!=w['organization_id']: return False,'Different organization'
        if w['owner_type']=='user' and not self.rows('SELECT 1 FROM organization_memberships WHERE organization_id=? AND user_id=?',(w['organization_id'],w['owner_id'])):
            return False,'Worker owner no longer belongs to the organization'
        if w['owner_type']=='user' and not self.rows('SELECT 1 FROM memberships WHERE user_id=? AND project_id=?',(w['owner_id'],project)) and not self.is_admin(w['owner_id'],p['organization_id']):
            return False,'Worker owner no longer has project access'
        if user:
            try: self.write_access(user,project)
            except PermissionError: return False,'Project membership required'
        return True,'Approved for this project'
    def worker_health(self,w,cached=False):
        key=(w['id'],w['execution_mode'],w.get('native_profile'),w.get('state'),bool(w.get('opt_in')))
        if not cached:
            health=self.adapter(w).health()
            self.health_cache[key]=(time.monotonic(),health)
            return health
        prior=self.health_cache.get(key)
        if prior and time.monotonic()-prior[0]<(0.5 if prior[1].get('status')=='busy' else 5):return prior[1]
        # Keep showing the last result while it refreshes; an actual start
        # always runs its own uncached check.
        if key not in self.health_jobs and not self.closing:
            adapter=self.adapter(dict(w))
            def probe():
                try:health=adapter.health()
                except Exception:health={'available':False,'status':'unavailable','reason':'Execution readiness check failed; inspect setup before retrying'}
                with self.lock:
                    self.health_cache[key]=(time.monotonic(),health)
                    self.health_jobs.pop(key,None)
            job=threading.Thread(target=probe,name='relay-health',daemon=True)
            self.health_jobs[key]=job;job.start()
        if prior:return prior[1]
        return {'available':False,'connected':False,'status':'checking','reason':'Checking authentication, runtime and sandbox readiness in the background'}
    def _quota_recovered(self,w,health):
        """Lift a provider-reported usage block once the provider allows usage again."""
        if w['state']!='quota_blocked' or not health.get('quota_recovered'):return w
        # An owner who set quota_blocked by hand keeps that decision.
        if self.one('SELECT source FROM capacity_sources WHERE worker_id=?',(w['id'],))['source']!='provider_reported':return w
        state='available' if w['opt_in'] else 'paused'
        self._capacity(w['id'],state,source='provider_reported')
        self.audit(w['owner_id'],'worker_quota_recovered',w['id'],metadata={'usage':health.get('usage')})
        return {**w,'state':state}
    def eligibility(self,w,project,user=None,health=None):
        approved,reason=self.approval(w,project,user)
        if not approved: return False,reason
        if w['state']=='quota_blocked':
            health=self.adapter(w).health() if health is None else health
            w=self._quota_recovered(w,health)
        if w['state']!='available': return False,'Capacity is '+w['state']
        if self.db.execute("SELECT COUNT(*) FROM assignments WHERE worker_id=? AND status='active'",(w['id'],)).fetchone()[0]>=w['max_parallel_tasks']:
            return False,'Worker concurrency limit reached'
        health=self.adapter(w).health() if health is None else health
        if not health.get('available'): return False,health.get('reason','Execution unavailable')
        return True,'Approved for this project'
    def worker_assignments(self,user,worker):
        visible=[]
        for assignment in self.rows("SELECT p.id,p.name,t.id AS task_id,t.state FROM tasks t JOIN conversations c ON c.id=t.conversation_id JOIN projects p ON p.id=c.project_id WHERE t.worker_id=? AND t.state NOT IN ('completed','failed','cancelled')",(worker,)):
            try:self.access(user,assignment['id']);visible.append(assignment)
            except PermissionError:pass
        return visible
    def public_worker(self,user,w,project=None,cached=False):
        owner=self.one('SELECT name FROM organizations WHERE id=?',(w['owner_id'],))['name'] if w['owner_type']=='org' else self.user(w['owner_id'])['name']
        allowed=[r['project_id'] for r in self.rows('SELECT project_id FROM worker_projects WHERE worker_id=?',(w['id'],))]
        # Never reveal grants to projects the reader cannot inspect.
        visible=[]
        for pid in allowed:
            try: self.access(user,pid); visible.append(pid)
            except PermissionError: pass
        managed=self.is_admin(user,w['organization_id']) or (w['owner_type']=='user' and w['owner_id']==user)
        health=self.worker_health(w,cached)
        w=self._quota_recovered(w,health)
        eligible,reason=self.eligibility(w,project,user,health) if project else (False,'Select a project to check eligibility')
        public={k:v for k,v in w.items() if k!='credential_ref'}
        public.update({'display_name':w['name'],'status':w['state'],'owner_name':owner,'opt_in':bool(w['opt_in']),'can_manage':managed,'permitted_project_ids':visible,'capacity':self.one('SELECT * FROM capacity_sources WHERE worker_id=?',(w['id'],)),'authorized':self.approval(w,project,user)[0] if project else False,'eligible':eligible,'eligibility_reason':reason,'health':health,'last_seen_at':now() if health.get('available') else None,'last_seen_source':'local adapter health check'})
        public['assigned_projects']=self.worker_assignments(user,w['id'])
        public['observation']=self.management.observation(user,w['id'])
        public['last_seen_at']=(public['observation']['last_action'] or {}).get('timestamp')
        public['last_seen_source']='last recorded observable event'
        project_blocked=bool(project and (self.rows('SELECT 1 FROM human_workspace_locks WHERE project_id=?',(project,)) or self.rows("SELECT 1 FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled','queued')",(project,))))
        public['can_accept_task']=eligible and not project_blocked
        public['assignment_reason']='Project is reserved by a human or unfinished task; queue explicitly or finish/checkpoint and release first' if project_blocked else reason
        if not w['opt_in'] and w['state']=='available':public['status']='paused'
        if not public['observation']['current_task'] and w['state']=='available' and not health.get('available'):
            public['status']=health.get('status','unavailable')
        return public
    def workers(self,user,project=None,mine=False,cached=False):
        self.org_member(user)
        if project: self.access(user,project)
        all_workers=self.rows("SELECT * FROM workers WHERE organization_id='orgdemo' AND removed_at IS NULL ORDER BY id")
        selected=[]
        for w in all_workers:
            if (not self.native_enabled and w['execution_mode']=='native') or (not self.demo and w['execution_mode']=='personal_supported'):continue
            owner=w['owner_type']=='user' and w['owner_id']==user
            shared=bool(self.rows('SELECT 1 FROM worker_projects w JOIN memberships m ON m.project_id=w.project_id WHERE w.worker_id=? AND m.user_id=?',(w['id'],user)))
            if mine and not owner and not (w['owner_type']=='org' and self.is_admin(user)): continue
            if owner or shared or self.is_admin(user): selected.append(self.public_worker(user,w,project,cached))
        return {'workers':selected,'personal_subscription':{'supported':False,'reason':PERSONAL_UNSUPPORTED_REASON}}
    def enroll(self,user,data):
        self.org_member(user); mode=data.get('execution_mode','simulation'); owner_type=data.get('owner_type','user')
        if any(field in data for field in ('owner_id','organization_id','backend')):
            raise ValueError('Execution owner and backend are assigned by the server')
        if mode not in ('simulation','organization_api','personal_supported','native'): raise ValueError('Unknown execution mode')
        if (mode=='native' and not self.native_enabled) or (mode=='personal_supported' and not self.demo):raise UnsupportedExecutionMode('Native workers require authenticated local-owner mode; shared/invite and demo dashboards cannot invoke personal subscriptions')
        if owner_type not in ('user','org'): raise ValueError('Unknown owner type')
        if owner_type=='org' and not self.is_admin(user): raise PermissionError('Only an administrator can enroll organization-owned workers')
        if mode=='organization_api' and owner_type!='org': raise ValueError('Organization API execution requires an organization-owned worker')
        if mode=='native':
            from .native import PROFILE_IDS
            if owner_type!='user' or user!=self.native_actor():raise PermissionError('Only the configured local native owner may enroll this profile')
            if data.get('native_profile') not in PROFILE_IDS:raise ValueError('Native profile must be a, b or c')
            if self.rows("SELECT 1 FROM workers WHERE execution_mode='native' AND native_profile=? AND removed_at IS NULL",(data['native_profile'],)):raise ValueError('Native profile already enrolled')
            health=self.adapter({'id':'profile-'+data['native_profile'],'execution_mode':'native','native_profile':data['native_profile'],'owner_type':'user','owner_id':user,'state':'available'}).health()
            if not health.get('available'):raise ValueError('Native profile is not ready: '+health.get('reason','Preflight failed'))
        name=data.get('display_name','').strip()
        if not name or len(name)>100: raise ValueError('Worker name must contain 1–100 characters')
        # One writer per worker in this MVP. Reject rather than claim concurrency support.
        if data.get('max_parallel_tasks',1)!=1: raise ValueError('This MVP supports one parallel task per worker')
        projects=data.get('project_ids',[])
        if not isinstance(projects,list): raise ValueError('project_ids must be a list')
        for p in projects: self.write_access(user,p)
        worker=uid(); owner='orgdemo' if owner_type=='org' else user
        self._insert_worker(worker,name,owner_type,owner,mode,bool(data.get('opt_in',True)),1,'env:RELAY_ORG_OPENAI_API_KEY' if mode=='organization_api' else None)
        if mode=='native':
            self.db.execute('UPDATE workers SET native_profile=? WHERE id=?',(data['native_profile'],worker))
            self.db.execute("UPDATE capacity_sources SET quota_scope=?,quota_owner=?,source='manual',limit_kind='chatgpt_subscription' WHERE worker_id=?",('native-profile:'+data['native_profile'],user,worker))
        for p in projects: self._grant(user,worker,p)
        self.audit(user,'worker_enrolled',worker,metadata={'execution_mode':mode,'owner_type':owner_type})
        return self.public_worker(user,self.one('SELECT * FROM workers WHERE id=?',(worker,)),projects[0] if projects else None)
    def configure(self,user,data,revoke=False):
        w=self.one('SELECT * FROM workers WHERE id=?',(data['worker_id'],)); self.manage(user,w)
        if w['removed_at']:raise ValueError('Worker enrollment was removed; enroll it again explicitly')
        if 'project_ids' in data:
            if not isinstance(data['project_ids'],list): raise ValueError('project_ids must be a list')
            for p in data['project_ids']: self.write_access(user,p)
        if 'state' in data and data['state'] not in ('available','paused','offline','quota_blocked'): raise ValueError('Invalid availability')
        if w['execution_mode']=='personal_supported' and data.get('state') not in (None,'offline'): raise UnsupportedExecutionMode(PERSONAL_UNSUPPORTED_REASON)
        if 'display_name' in data and (not isinstance(data['display_name'],str) or not data['display_name'].strip() or len(data['display_name'])>100): raise ValueError('Invalid worker name')
        if 'max_parallel_tasks' in data and data['max_parallel_tasks']!=1: raise ValueError('This MVP supports one parallel task per worker')
        if revoke and data.get('project_id'): self.access(user,data['project_id'])
        if 'display_name' in data: self.db.execute('UPDATE workers SET name=?,updated_at=? WHERE id=?',(clean(data['display_name']),now(),w['id']))
        if 'opt_in' in data or (revoke and not data.get('project_id')):
            self.db.execute('UPDATE workers SET opt_in=?,updated_at=? WHERE id=?',(0 if revoke else int(bool(data['opt_in'])),now(),w['id']))
        if 'project_ids' in data:
            self.db.execute('DELETE FROM worker_projects WHERE worker_id=?',(w['id'],))
            for p in data['project_ids']: self._grant(user,w['id'],p)
        if revoke and data.get('project_id'): self.db.execute('DELETE FROM worker_projects WHERE worker_id=? AND project_id=?',(w['id'],data['project_id']))
        active=self.rows("SELECT * FROM tasks WHERE worker_id=? AND state IN ('running','pausing')",(w['id'],))
        requested_state=data.get('state')
        if requested_state and (not active or requested_state!='available'): self._capacity(w['id'],requested_state)
        self.audit(user,'worker_revoked' if revoke else 'worker_configured',w['id'],data.get('project_id'),{'opt_in':data.get('opt_in'),'availability':requested_state,'project_ids':data.get('project_ids')})
        current=self.one('SELECT * FROM workers WHERE id=?',(w['id'],))
        for t in active:
            project=self.project(t)
            revoked=not current['opt_in'] or not self.rows('SELECT 1 FROM worker_projects WHERE worker_id=? AND project_id=?',(w['id'],project)) or requested_state in ('paused','offline','quota_blocked')
            if revoked: self._request_handoff(user,t,None,'worker_opt_out' if not current['opt_in'] else 'eligibility_revoked','Worker owner/admin revoked execution eligibility',check_access=False)
        return self.public_worker(user,self.one('SELECT * FROM workers WHERE id=?',(w['id'],)))

    # Persistent working context and Git checkpoints, separately from the full transcript.
    def checkpoint(self,t,reason):
        self.assert_no_human_writer(self.project(t))
        project=self.project(t); uncommitted=self.ws.status(project)
        commit=self.ws.checkpoint(project,reason)
        self.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?,?,?)',(uid(),project,t['conversation_id'],t['id'],t['worker_id'],commit,now(),reason,uncommitted))
        self.event(t,'checkpoint',commit,metadata={'reason':reason,'uncommitted_changes':uncommitted})
        return commit
    def context(self,t,checkpoint):
        # Fetch a bounded window; older event bodies remain available on demand.
        events=self.rows('SELECT * FROM events WHERE conversation_id=? ORDER BY sequence DESC LIMIT 30',(t['conversation_id'],))[::-1]
        version=self.db.execute('SELECT COUNT(*)+1 FROM contexts WHERE task_id=?',(t['id'],)).fetchone()[0]
        view=self.ws.inspect(self.project(t)); progress=json.loads(t.get('progress_json') or '{}'); tests=next((e['content'][-4000:] for e in reversed(events) if e['event_type']=='tool_output'),'Tests not run yet')
        data={'version':version,'objective':t['objective'],'user_instructions':t['objective'],'conversation_id':t['conversation_id'],'previous_worker':t['worker_id'],'checkpoint':checkpoint,
            'decisions':['Execute only within the approved project workspace','Preserve full original events separately from this lossy summary'],
            'completed_work':progress.get('completed_work',(['validator.py implementation written'] if t['execution_mode']=='simulation' and 'validator.py' in view['files'] else [])),
            'worker_progress':progress,'execution_phase':t.get('execution_phase',0),
            'active_branch':self.ws.git(self.ws.path(self.project(t)),'rev-parse','--abbrev-ref','HEAD'),
            'constraints':['Only one writer lease per project','Worker owner approval and project membership required','No credentials or hidden reasoning in handoffs'],
            'files_changed':list(view['files']),'uncommitted_changes':self.ws.status(self.project(t)),
            'todos':[] if t['state']=='completed' else (['Add validator tests and run unittest'] if t['execution_mode']=='simulation' else ['Continue unfinished objective; inspect files and latest errors before editing']),
            'test_results':progress.get('test_results',[]),'limitations':progress.get('limitations',[]),
            'tests':tests,'errors':[e['content'][:1000] for e in events if e['event_type']=='error'][-3:],
            'history_directory':'.relay/history (read referenced event JSON files on demand)',
            'recent_turns':[{'sequence':e['sequence'],'worker_id':e['worker_id'],'content':e['content'][:600]} for e in events[-5:]],
            'older_event_endpoint':f"/api/conversations/{t['conversation_id']}/events?after=0&limit=100",'source_range':[events[0]['sequence'],events[-1]['sequence']] if events else [0,0]}
        if progress.get('pending_steps') and t['state']!='completed': data['todos']=progress['pending_steps']
        if progress.get('decisions'): data['decisions']+=progress['decisions']
        if t['execution_mode'] in LIVE_MODES:
            base=self.rows('SELECT git_commit FROM workspace_checkpoints WHERE task_id=? ORDER BY rowid LIMIT 1',(t['id'],))
            data['files_changed']=self.ws.git(self.ws.path(self.project(t)),'diff','--name-only',base[0]['git_commit'],'HEAD','--','.',':(exclude).relay').splitlines() if base else []
        data=scrub(data)
        self.db.execute('INSERT INTO contexts VALUES(?,?,?,?)',(uid(),t['id'],version,json.dumps(data)))
        self.ws.write_handoff(self.project(t),data)
        self.ws.write_history(self.project(t),self.rows('SELECT * FROM events WHERE conversation_id=? ORDER BY sequence',(t['conversation_id'],)))
        self.event(t,'context',f'Working context v{version} saved',metadata={'version':version,'source_range':data['source_range']})
        return data
    def _latest_context(self,t):
        rows=self.rows('SELECT data FROM contexts WHERE task_id=? ORDER BY version DESC LIMIT 1',(t['id'],))
        if rows: return json.loads(rows[0]['data'])
        return self.context(t,self.ws.inspect(self.project(t))['checkpoint'])

    # Scheduler: every assignment checks grant, mode, capacity and the workspace lease.
    def available(self,project=None,user=None,exclude=None,mode=None):
        if project is None: return []
        candidates=[]
        for w in self.rows("SELECT * FROM workers WHERE state='available' AND removed_at IS NULL AND id!=? ORDER BY id",(exclude or '',)):
            if mode and w['execution_mode']!=mode: continue
            if self.eligibility(w,project,user)[0]: candidates.append(w)
        return candidates
    def _candidate(self,user,project,source=None,target=None,mode=None):
        if target:
            w=self.one('SELECT * FROM workers WHERE id=?',(target,))
            if w['execution_mode']=='personal_supported': raise UnsupportedExecutionMode(PERSONAL_UNSUPPORTED_REASON)
            eligible,reason=self.eligibility(w,project,user)
            if not eligible: raise PermissionError('Worker is not eligible: '+reason)
            if target==source: raise ValueError('Choose a different worker')
            if mode and w['execution_mode']!=mode: raise ValueError('Handoff requires the same execution mode; live tasks never fall back to simulation')
            return w
        options=self.available(project,user,source,mode)
        return options[0] if options else None
    def assign(self,t,w,authorized_by,context=None):
        # Caller owns process lock; SQLite makes lease+assignment atomic.
        self.assert_no_human_writer(self.project(t))
        self.management._project_idle(self.project(t),t['id'])
        approved,reason=self.eligibility(w,self.project(t),authorized_by)
        if not approved: raise PermissionError('Worker assignment no longer permitted: '+reason)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO workspace_leases VALUES(?,?,?,?,?)',(self.project(t),t['id'],w['id'],uid(),now()))
            self.db.execute('UPDATE tasks SET worker_id=?,execution_mode=?,last_error=NULL WHERE id=?',(w['id'],w['execution_mode'],t['id']))
            t['worker_id']=w['id']; t['execution_mode']=w['execution_mode']
            self._capacity(w['id'],'busy')
            self.db.execute('INSERT INTO assignments(id,task_id,worker_id,session_id,created_at,authorized_by,context_version,status) VALUES(?,?,?,?,?,?,?,?)',(uid(),t['id'],w['id'],('simulated-' if w['execution_mode']=='simulation' else 'codex-')+uid(),now(),authorized_by,context.get('version') if context else None,'active'))
            self.event(t,'assignment',f"Assigned {w['name']} (authorized by {authorized_by})",metadata={'authorized_by':authorized_by,'execution_mode':w['execution_mode'],'simulated':w['execution_mode']=='simulation','native_thread_id':None})
            self.state(t,'running'); self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK'); raise
    def _release(self,t,status='released',capacity=None):
        self.db.execute('DELETE FROM workspace_leases WHERE task_id=?',(t['id'],))
        self.db.execute("UPDATE assignments SET status=?,finished_at=? WHERE task_id=? AND status='active'",(status,now(),t['id']))
        if t['worker_id']:
            w=self.one('SELECT * FROM workers WHERE id=?',(t['worker_id'],))
            # Never reverse an owner's offline/quota-blocked decision.
            state=capacity or ('available' if w['opt_in'] else 'paused')
            if w['state'] in ('offline','quota_blocked','unsupported'): state=w['state']
            other_active=self.rows("SELECT 1 FROM assignments WHERE worker_id=? AND status='active' AND task_id!=?",(t['worker_id'],t['id']))
            if not other_active and state!=w['state']: self._capacity(t['worker_id'],state)
    def submit(self,user,conversation,prompt,worker_id=None,execution_mode=None,pause_after_turn=False,native_phases=2):
        c=self.one('SELECT * FROM conversations WHERE id=?',(conversation,)); self.write_access(user,c['project_id'])
        if c['archived_at']:raise ValueError('Restore the conversation before starting work')
        self.assert_no_human_writer(c['project_id'])
        if self.rows("SELECT 1 FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled')",(c['project_id'],)):
            raise ValueError('Finish or cancel the active task in this project before starting another conversation task')
        if native_phases not in (2,3):raise ValueError('native_phases must be 2 or 3')
        if not isinstance(pause_after_turn,bool): raise ValueError('pause_after_turn must be boolean')
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>8000: raise ValueError('Prompt must contain 1–8000 characters')
        if self.rows("SELECT 1 FROM tasks WHERE conversation_id=? AND state NOT IN ('completed','failed','cancelled')",(conversation,)): raise ValueError('Finish or cancel the active task first')
        if execution_mode=='personal_supported': raise UnsupportedExecutionMode(PERSONAL_UNSUPPORTED_REASON)
        if execution_mode not in (None,'simulation','organization_api','native'): raise ValueError('Unknown execution mode')
        fixture='password' in prompt.lower() and 'validator' in prompt.lower()
        if worker_id:
            requested=self.one('SELECT * FROM workers WHERE id=?',(worker_id,))
            if requested['execution_mode']=='personal_supported': raise UnsupportedExecutionMode(PERSONAL_UNSUPPORTED_REASON)
            approved,reason=self.approval(requested,c['project_id'],user)
            if not approved: raise PermissionError('Worker is not approved: '+reason)
            if execution_mode and execution_mode!=requested['execution_mode']: raise ValueError('Worker does not support the requested execution mode')
            mode=requested['execution_mode']
            if self.eligibility(requested,c['project_id'],user)[0]: w=requested
            elif mode in LIVE_MODES: w=None  # Save authorized API intent until runtime capacity is ready.
            else: raise PermissionError('Worker is not eligible: '+self.eligibility(requested,c['project_id'],user)[1])
        else:
            approved_api=any(self.approval(candidate,c['project_id'],user)[0] for candidate in self.rows("SELECT * FROM workers WHERE execution_mode='organization_api'"))
            mode=execution_mode or ('organization_api' if not fixture and approved_api else 'simulation')
            w=self._candidate(user,c['project_id'],mode=mode)
        if mode=='simulation' and not fixture: raise ValueError('Simulated workers support the password-strength validator demo only.')
        t={'id':uid(),'conversation_id':conversation,'state':'queued','worker_id':None,'requested_worker_id':worker_id or (w['id'] if w else None),'objective':clean(prompt),'generation':0,'requested_by':user,'execution_mode':mode,'last_error':None,'pending_action':None,'pending_target':None,'pending_reason':None,'pending_authorized_by':None,'pause_after_turn':int(pause_after_turn),'execution_phase':0,'progress_json':'{}','native_phases':native_phases}
        fields=','.join(t); self.db.execute(f"INSERT INTO tasks({fields}) VALUES({','.join('?' for _ in t)})",tuple(t.values()))
        self.event(t,'prompt',prompt,'user',{'user_id':user}); self.state(t,'queued')
        if not w:
            self.event(t,'queue','No approved available '+mode+' worker. Task and objective saved; use Switch Worker when capacity is available.',metadata={'execution_mode':mode,'requested_worker_id':worker_id})
            return t
        self.assign(t,w,user)
        context={}
        if mode in LIVE_MODES:
            try: context=self.context(t,self.checkpoint(t,'Before Codex execution'))
            except Exception as exc:
                self._execution_error(t,exc)
                return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
        self._execute(t,w,False,context)
        return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
    def _emit(self,task_id,kind,content,metadata=None):
        with self.lock:
            t=self.one('SELECT * FROM tasks WHERE id=?',(task_id,))
            metadata=dict(metadata or {}); metadata['origin']='codex' if t['execution_mode'] in LIVE_MODES else 'simulation'
            job=self.jobs.get(task_id,{})
            if kind=='continuation_checkpoint':
                # This callback is synchronous: the adapter cannot start its
                # next turn until Git and durable working context are saved.
                if t.get('pending_action') or self.closing or job.get('cancel',threading.Event()).is_set():
                    return {'checkpoint':self.ws.git(self.ws.path(self.project(t)),'rev-parse','HEAD'),'stopping':True}
                t['progress_json']=json.dumps(scrub(metadata.get('progress',{})))
                t['execution_phase']=t.get('execution_phase',0)+1
                self.db.execute('UPDATE tasks SET progress_json=?,execution_phase=? WHERE id=?',(t['progress_json'],t['execution_phase'],task_id))
                checkpoint=self.checkpoint(t,'Intermediate native progress')
                return self.context(t,checkpoint)
            if job.get('run_id'): metadata['execution_run_id']=job['run_id']
            if kind=='session_started' and metadata.get('native_thread_id'):
                native=clean(metadata['native_thread_id'])
                self.db.execute("UPDATE assignments SET native_thread_id=? WHERE task_id=? AND status='active'",(native,task_id))
                if job.get('run_id'): self.db.execute('UPDATE execution_runs SET native_thread_id=? WHERE id=?',(native,job['run_id']))
            self.event(t,kind,content,'assistant' if kind=='response' else 'system',metadata)
    def _execute(self,t,w,resume,context):
        assignment=self.one("SELECT * FROM assignments WHERE task_id=? AND status='active' ORDER BY rowid DESC LIMIT 1",(t['id'],))
        approved,reason=self.approval(w,self.project(t),assignment['authorized_by'] or t['requested_by'])
        if not approved: raise PermissionError('Worker execution no longer authorized: '+reason)
        adapter=self.adapter(w); cancelled=threading.Event(); run_id=uid(); started=time.monotonic()
        self.db.execute('INSERT INTO execution_runs(id,task_id,assignment_id,worker_id,execution_mode,state,started_at) VALUES(?,?,?,?,?,?,?)',(run_id,t['id'],assignment['id'],w['id'],w['execution_mode'],'running',now()))
        self.event(t,'execution_started','Codex execution started' if w['execution_mode'] in LIVE_MODES else 'Simulated execution started',metadata={'execution_run_id':run_id,'origin':'codex' if w['execution_mode'] in LIVE_MODES else 'simulation'})
        workspace=self.ws.path(self.project(t))
        if resume: self.event(t,'context_loaded',f"Loaded context v{context.get('version')} for the same Relay conversation",metadata={'context_received':context})
        def execute():
            try:
                fn=adapter.resume if resume else adapter.start
                result=fn(t,workspace,context,lambda kind,content,metadata=None:self._emit(t['id'],kind,content,metadata),cancelled)
                with self.lock:
                    current=self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
                    self.db.execute('UPDATE execution_runs SET state=?,finished_at=?,exit_code=?,duration_ms=?,native_thread_id=?,error_category=?,retry_after_seconds=?,summary=?,progress_json=? WHERE id=?',(result.state,now(),getattr(result,'exit_code',None),getattr(result,'duration_ms',0) or int((time.monotonic()-started)*1000),clean(result.native_thread_id) if result.native_thread_id else None,getattr(result,'error_category',None),getattr(result,'retry_after_seconds',None),clean(result.summary),json.dumps(scrub(getattr(result,'progress',{}))),run_id))
                    if getattr(result,'progress',None):
                        current['progress_json']=json.dumps(scrub(result.progress)); self.db.execute('UPDATE tasks SET progress_json=? WHERE id=?',(current['progress_json'],t['id']))
                    self.event(current,'execution_result',result.summary,metadata={'execution_run_id':run_id,'state':result.state,'exit_code':getattr(result,'exit_code',None),'duration_ms':getattr(result,'duration_ms',0),'error_category':getattr(result,'error_category',None),'origin':'codex' if w['execution_mode'] in LIVE_MODES else 'simulation'})
                    if result.native_thread_id:
                        self.db.execute("UPDATE assignments SET native_thread_id=? WHERE task_id=? AND status='active'",(clean(result.native_thread_id),t['id']))
                    if not getattr(result,'safe_to_release',True):
                        self.unsafe_tasks.add(t['id'])
                        self.state(current,'pausing')
                        self.db.execute('UPDATE tasks SET last_error=? WHERE id=?',('Execution process cleanup unverified; writer lease retained. Review the runtime before restarting Relay.',t['id']))
                        self.event(current,'error','Execution process cleanup could not be verified. No replacement worker may write until cleanup is confirmed.')
                        return
                    if result.state=='quota_blocked': self._block_quota(current,w,result)
                    if self.closing:
                        self.db.execute("UPDATE tasks SET state='handoff_pending',pending_action=NULL WHERE id=?",(t['id'],))
                        self.event(current,'stop','Backend shutting down; execution stopped without reassigning or replaying tools.')
                        self._release(current)
                        self.jobs.pop(t['id'],None)
                        return
                    if result.state=='quota_blocked':
                        self.jobs.pop(t['id'],None)
                        if current['pending_action']=='cancel': self._cancel_stopped(current,current['pending_authorized_by'])
                        else:
                            # Recorded as the task blocker so every view reports the provider limit.
                            self.db.execute('UPDATE tasks SET pending_action=NULL,last_error=? WHERE id=?',('Provider usage limit reported: '+clean(result.summary)[:500],t['id']))
                            self._complete_handoff(current,None,'provider_limit','Provider reported capacity unavailable. Wait for this quota scope; no automatic native account rotation.' if current['execution_mode']=='native' else 'Provider reported shared organization API exhaustion; wait or resolve billing before retry.',current['pending_authorized_by'] or current['requested_by'])
                        return
                    if current['pending_action']:
                        if result.state=='cancelled':
                            self.event(current,'interruption','The previous tool was interrupted. Current files may be partial; inspect the diff and rerun tests before completion.')
                        self.jobs.pop(t['id'],None)
                        self._finish_pending_action(current)
                        return
                    if result.state=='running':
                        checkpoint=self.checkpoint(current,'Worker implementation')
                        self.context(current,checkpoint)
                    else:
                        checkpoint=self.checkpoint(current,'Task '+result.state)
                        boundary=(w['execution_mode'] in LIVE_MODES and result.state=='completed' and current.get('pause_after_turn') and current.get('execution_phase',0)<current.get('native_phases',2)-1)
                        pending=(w['execution_mode'] in LIVE_MODES and result.state=='completed' and bool(getattr(result,'progress',{}).get('pending_steps')))
                        retryable=getattr(result,'error_category',None) in ('timeout','transport_error','provider_unavailable') or (w['execution_mode']=='native' and result.state=='failed')
                        if w['execution_mode']=='native' and result.state=='failed':self.db.execute('UPDATE tasks SET last_error=? WHERE id=?',(clean(result.summary),t['id']))
                        if boundary or pending or retryable or result.state=='blocked':
                            current['execution_phase']=current.get('execution_phase',0)+1; self.db.execute('UPDATE tasks SET execution_phase=? WHERE id=?',(current['execution_phase'],t['id']))
                            self.state(current,'handoff_pending')
                            reason=('Explicit handoff boundary saved.' if boundary else result.summary if result.state=='blocked' else 'Execution stopped; review preserved work before an explicit retry.')
                            self.event(current,'pause_boundary',reason)
                            if not boundary:
                                self.db.execute('UPDATE tasks SET last_error=? WHERE id=?',(clean(result.summary),t['id']))
                        else: self.state(current,result.state if result.state in TERMINAL else 'failed')
                        self.event(current,'response',result.summary,'assistant')
                        self.context(current,checkpoint); self._release(current)
                    self.jobs.pop(t['id'],None)
            except Exception as exc:
                with self.lock:
                    current=self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
                    self.db.execute('UPDATE execution_runs SET state=?,finished_at=?,error_category=?,summary=? WHERE id=?',('failed',now(),'runtime_error',clean(str(exc))[:2000],run_id))
                    self._execution_error(current,exc)
                    self.jobs.pop(t['id'],None)
        self.jobs[t['id']]={'cancel':cancelled,'adapter':adapter,'thread':None,'run_id':run_id}
        if w['execution_mode'] in LIVE_MODES:
            thread=threading.Thread(target=execute,name='relay-task-'+t['id'],daemon=True); self.jobs[t['id']]['thread']=thread; thread.start()
        else: execute()
    def _block_quota(self,t,w,result):
        category=getattr(result,'error_category',None) or 'provider_error'
        scope=self.one('SELECT quota_scope FROM capacity_sources WHERE worker_id=?',(w['id'],))['quota_scope']
        for peer in self.rows('SELECT worker_id FROM capacity_sources WHERE quota_scope=?',(scope,)):
            self._capacity(peer['worker_id'],'quota_blocked',source='provider_reported',limit_kind=category)
        guidance='Wait for this ChatGPT account’s capacity to recover; retry explicitly after reviewing preserved work.' if w['execution_mode']=='native' else 'Wait for capacity or resolve organization billing; retry explicitly after reviewing preserved work.'
        self.event(t,'capacity_blocked','Native profile quota is blocked; no account switching is performed to bypass limits.' if w['execution_mode']=='native' else 'Shared API quota is blocked. A different worker on this scope cannot reset the limit.',metadata={'category':category,'quota_scope':scope,'retry_after_seconds':getattr(result,'retry_after_seconds',None),'retry_guidance':guidance})
    def _execution_error(self,t,exc):
        message=clean(str(exc))[:2000]
        self.event(t,'error','Execution paused after an error: '+message,metadata={'recoverable':True})
        if t['execution_mode'] in LIVE_MODES and t['worker_id']:
            try: safe=self.adapter(self.one('SELECT * FROM workers WHERE id=?',(t['worker_id'],))).recover(t['id'])
            except Exception: safe=False
            if not safe:
                self.unsafe_tasks.add(t['id'])
                self.db.execute("UPDATE tasks SET state='pausing',last_error=? WHERE id=?",('Container cleanup unverified after error. Restore Docker and restart Relay.',t['id']))
                t['state']='pausing'
                return
        self.db.execute("UPDATE tasks SET state='handoff_pending',last_error=?,pending_action=NULL WHERE id=?",(message,t['id']))
        t['state']='handoff_pending'; self._release(t,status='interrupted')
        # Preserve working files. Never reset/check out or silently replay a tool.
    def _request_handoff(self,user,t,target,reason_code,reason,check_access=True):
        if check_access: self.access(user,self.project(t))
        if t['id'] in self.unsafe_tasks: raise ValueError('Container cleanup is unverified. Restore Docker and restart Relay before resuming.')
        if t['state'] not in ('running','queued','handoff_pending','pausing'): raise ValueError('Task cannot switch in this state')
        if target: self._candidate(user,self.project(t),t['worker_id'],target,t['execution_mode'])
        if t['state']=='pausing': return t
        self.db.execute('UPDATE tasks SET generation=generation+1 WHERE id=?',(t['id'],)); t['generation']+=1
        if t['id'] in self.jobs and self.jobs[t['id']]['thread'] is not None:
            self.state(t,'pausing')
            self.db.execute('UPDATE tasks SET pending_action=?,pending_target=?,pending_reason=?,pending_authorized_by=? WHERE id=?',(reason_code,target,reason,user,t['id']))
            job=self.jobs[t['id']]; job['cancel'].set(); job['adapter'].cancel(t['id'])
            self.event(t,'stop','Waiting for the current tool to stop safely before checkpointing.')
            return t
        return self._complete_handoff(t,target,reason_code,reason,user)
    def _finish_pending_action(self,t):
        action=t['pending_action']; self.db.execute('UPDATE tasks SET pending_action=NULL WHERE id=?',(t['id'],))
        if action=='cancel': self._cancel_stopped(t,t['pending_authorized_by'])
        else: self._complete_handoff(t,t['pending_target'],action,t['pending_reason'],t['pending_authorized_by'])
    def _complete_handoff(self,t,target,code,reason,user):
        source=t['worker_id']; handoff=uid()
        if source and t.get('pause_after_turn'):
            latest=self.rows('SELECT state FROM execution_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(t['id'],))
            has_lease=bool(self.rows('SELECT 1 FROM workspace_leases WHERE task_id=?',(t['id'],)))
            if has_lease and latest and latest[0]['state'] in ('running','cancelled'):
                t['execution_phase']=t.get('execution_phase',0)+1; self.db.execute('UPDATE tasks SET execution_phase=? WHERE id=?',(t['execution_phase'],t['id']))
        self.state(t,'pausing'); self.event(t,'stop',reason)
        try:
            checkpoint=self.checkpoint(t,'Handoff checkpoint'); self.state(t,'checkpointed'); context=self.context(t,checkpoint)
        except Exception as exc:
            self._execution_error(t,exc)
            self.db.execute('INSERT INTO handoffs(id,task_id,source,target,reason,checkpoint,context_version,timestamp,status,reason_code,authorized_by,result,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(handoff,t['id'],source,target,reason,None,None,now(),'failed',code,user,'recoverable',clean(str(exc))[:1000]))
            self.audit(user,'handoff_failed',source,self.project(t),{'reason_code':code})
            return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
        self._release(t); self.state(t,'handoff_pending')
        mode=t['execution_mode']
        w=None
        try:
            if code!='task_pause' and (mode!='native' or target):w=self._candidate(user,self.project(t),source,target,mode)
        except (PermissionError,ValueError): pass  # Eligibility can change while a real tool stops.
        self.db.execute('INSERT INTO handoffs(id,task_id,source,target,reason,checkpoint,context_version,timestamp,status,reason_code,authorized_by,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(handoff,t['id'],source,w['id'] if w else None,reason,checkpoint,context['version'],now(),'resumed' if w else 'pending',code,user,'assigned' if w else 'waiting_for_eligible_worker'))
        self.event(t,'handoff',f'{source} → {w["id"] if w else "waiting for approved available worker"}: {reason}',metadata={'handoff_id':handoff,'reason_code':code,'authorized_by':user,'checkpoint':checkpoint,'context_version':context['version'],'status':'resumed' if w else 'pending'})
        self.audit(user,'handoff_created',source,self.project(t),{'handoff_id':handoff,'target':w['id'] if w else None,'reason_code':code})
        if w:
            self.assign(t,w,user,context)
            self._execute(t,w,bool(source),context)
        return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
    def switch(self,user,task_id,generation,reason,target=None,reason_code='manual_switch'):
        t=self.task(user,task_id)
        self.write_access(user,self.project(t))
        self.assert_no_human_writer(self.project(t))
        self.management._project_idle(self.project(t),t['id'])
        if t['state']=='queued' and not target and t.get('requested_worker_id'):target=t['requested_worker_id']
        if t['generation']!=generation: return t
        if reason_code not in ('manual_switch','task_pause','retry','simulated_limit'): raise ValueError('Invalid switch reason')
        if t['state'] not in ('running','queued','handoff_pending','pausing'): raise ValueError('Task cannot switch in this state')
        if t['id'] in self.unsafe_tasks: raise ValueError('Container cleanup is unverified. Restore Docker and restart Relay before resuming.')
        if t['execution_mode']=='native' and t['worker_id'] and self.one('SELECT availability FROM capacity_sources WHERE worker_id=?',(t['worker_id'],))['availability']=='quota_blocked':
            raise ValueError('Native subscription quota is exhausted. Wait for that account; profile handoff cannot be used as a quota workaround.')
        if target: self._candidate(user,self.project(t),t['worker_id'],target,t['execution_mode'])
        if reason_code=='simulated_limit':
            if t['execution_mode']!='simulation': raise ValueError('Simulated limits apply only to simulated workers')
            if t['worker_id']: self._capacity(t['worker_id'],'quota_blocked',limit_kind='simulated_usage_limit')
        return self._request_handoff(user,t,target,reason_code,reason)
    def resume_task(self,user,t,generation):
        self.write_access(user,self.project(t))
        self.assert_no_human_writer(self.project(t))
        if t['generation']!=generation:return t
        if t['state']!='handoff_pending' or t['id'] in self.jobs:raise ValueError('Resume requires a safely paused task')
        w=self._candidate(user,self.project(t),target=t['worker_id'],mode=t['execution_mode']) if t['worker_id'] else None
        if not w:raise ValueError('Choose an authorized available worker using Request Handoff')
        t['generation']+=1;self.db.execute('UPDATE tasks SET generation=? WHERE id=?',(t['generation'],t['id']))
        context=self._latest_context(t);self.assign(t,w,user,context)
        self.audit(user,'task_resumed',w['id'],self.project(t),{'task_id':t['id']})
        self._execute(t,w,True,context)
        return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
    def _cancel_stopped(self,t,user):
        try:
            checkpoint=self.checkpoint(t,'User cancellation'); self.context(t,checkpoint)
        except Exception as exc: self.event(t,'error','Cancellation checkpoint failed; files retained: '+clean(str(exc)))
        self.state(t,'cancelled'); self.event(t,'stop',f'Cancelled by {user}'); self._release(t,'cancelled')
    def cancel(self,user,t):
        if t['state'] not in ACTIVE: raise ValueError('Task is not active')
        if t['state']=='queued' and t['id'] not in self.jobs:
            self.state(t,'cancelled');self.event(t,'stop',f'Queued task cancelled by {user}; no workspace changes')
            return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
        if t['id'] in self.unsafe_tasks: raise ValueError('Container cleanup is unverified. Restore Docker and restart Relay to verify shutdown.')
        if t['id'] in self.jobs and self.jobs[t['id']]['thread'] is not None:
            self.state(t,'pausing'); self.db.execute("UPDATE tasks SET pending_action='cancel',pending_authorized_by=? WHERE id=?",(user,t['id']))
            job=self.jobs[t['id']]; job['cancel'].set(); job['adapter'].cancel(t['id'])
        else: self._cancel_stopped(t,user)
        return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))
    def continue_task(self,user,t):
        if t['state']!='running': raise ValueError('Use Switch Worker to resume a pending task')
        if t['id'] in self.jobs: raise ValueError('Worker is already executing; wait for its tool or request a switch')
        w=self.one('SELECT * FROM workers WHERE id=?',(t['worker_id'],))
        # Existing lease is held; do not acquire a second writer on continuation.
        approved,reason=self.approval(w,self.project(t),user)
        if not approved: raise PermissionError('Worker execution eligibility was revoked: '+reason)
        self._execute(t,w,True,self._latest_context(t))
        return self.one('SELECT * FROM tasks WHERE id=?',(t['id'],))

    def overview(self,user,light=False):
        u=self.user(user); self.org_member(user)
        projects=self.rows("SELECT * FROM projects WHERE organization_id='orgdemo'") if self.is_admin(user) else self.rows('SELECT p.* FROM projects p JOIN memberships m ON m.project_id=p.id WHERE m.user_id=?',(user,))
        directory={'workers':[],'personal_subscription':{'supported':False,'reason':PERSONAL_UNSUPPORTED_REASON}} if light else self.workers(user)
        people=self.rows('SELECT id,name,role FROM users') if self.demo else self.rows('SELECT u.id,u.name,u.role FROM users u JOIN accounts a ON a.user_id=u.id WHERE a.disabled=0')
        for p in projects:
            p['role']='admin' if self.is_admin(user) else self.one('SELECT role FROM memberships WHERE user_id=? AND project_id=?',(user,p['id']))['role']
            p['can_write']=p['role']!='viewer' and self.org_member(user)['role']!='viewer'
        return {'user':u,'users':people,'organizations':self.rows('SELECT * FROM organizations'),'projects':projects,'workers':directory['workers'],
            'features':{'agent_management':True,'explorer_operations':True,'checkpoint_reviews':True,'automatic_workflow':True,'recent_history':True,'development_terminal':True,'live_agents':True,'editor_leases':True},
            'auth':{'mode':'local-owner' if self.local_owner_id else 'local_demo' if self.demo else 'invite','production_ready':not self.demo and not self.local_owner_id,'description':'Pinned local owner; loopback only' if self.local_owner_id else 'Seeded local identities' if self.demo else 'Invite-only Relay accounts; HTTPS required','native_enabled':self.native_enabled,'local_owner_id':self.local_owner_id},
            'personal_subscription':directory['personal_subscription'],
            'execution':{'mode':'mixed','connected':any(w['execution_mode'] in LIVE_MODES and w['health'].get('connected',False) for w in directory['workers']),'detail':'Native profiles use the local owner’s ChatGPT subscriptions and explicit handoffs. Simulated capacity is labeled; organization API capacity is shared by organization.'}}
    def detail(self,user,project,conversation=None,light=False):
        self.access(user,project)
        c=self.one('SELECT * FROM conversations WHERE project_id=? AND id=?',(project,conversation)) if conversation else self.one('SELECT * FROM conversations WHERE project_id=? ORDER BY rowid LIMIT 1',(project,))
        if light:
            return {'light':True,'conversation':c,'tasks':self.rows('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 100',(c['id'],)),
                'workspace':{'files':{},'diff':'','checkpoint':self.ws.git(self.ws.path(project),'rev-parse','HEAD')},'contexts':[],
                'handoffs':list(reversed(self.rows('SELECT h.* FROM handoffs h JOIN tasks t ON t.id=h.task_id WHERE t.conversation_id=? ORDER BY h.rowid DESC LIMIT 50',(c['id'],)))),
                'checkpoints':self.rows('SELECT * FROM workspace_checkpoints WHERE project_id=? ORDER BY rowid DESC LIMIT 100',(project,)),
                'execution_runs':[],'assignments':[],'memberships':self.rows('SELECT * FROM memberships WHERE project_id=?',(project,)),'workers':[]}
        workspace=self.ws.inspect(project)
        latest=self.rows('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(c['id'],))
        if latest and latest[0]['execution_mode'] in LIVE_MODES:
            base=self.rows('SELECT git_commit FROM workspace_checkpoints WHERE task_id=? ORDER BY rowid LIMIT 1',(latest[0]['id'],))
            if base:
                workspace['task_diff']=clean(self.ws.git(self.ws.path(project),'diff','--no-ext-diff','--no-textconv',base[0]['git_commit'],'HEAD','--','.',':(exclude).relay')[:100000])
        return {'conversation':c,'tasks':self.rows('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC',(c['id'],)),
            'workspace':workspace,'contexts':[json.loads(r['data']) for r in self.rows('SELECT c.* FROM contexts c JOIN tasks t ON t.id=c.task_id WHERE t.conversation_id=? ORDER BY c.rowid DESC LIMIT 10',(c['id'],))],
            'handoffs':self.rows('SELECT h.* FROM handoffs h JOIN tasks t ON t.id=h.task_id WHERE t.conversation_id=? ORDER BY h.rowid',(c['id'],)),
            'checkpoints':self.rows('SELECT * FROM workspace_checkpoints WHERE project_id=? ORDER BY rowid DESC',(project,)),
            'execution_runs':self.rows('SELECT * FROM execution_runs WHERE task_id IN (SELECT id FROM tasks WHERE conversation_id=?) ORDER BY rowid',(c['id'],)),
            'assignments':self.rows('SELECT a.* FROM assignments a JOIN tasks t ON t.id=a.task_id WHERE t.conversation_id=? ORDER BY a.rowid',(c['id'],)),
            'memberships':self.rows('SELECT * FROM memberships WHERE project_id=?',(project,)), 'workers':self.workers(user,project)['workers']}
    def events(self,user,conversation,after=0,limit=50,before=None,recent=False):
        c=self.one('SELECT * FROM conversations WHERE id=?',(conversation,)); self.access(user,c['project_id'])
        if recent or before is not None:
            if before is not None and before<1:raise ValueError('Invalid history cursor')
            rows=self.rows('SELECT * FROM events WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',(conversation,before if before is not None else 9223372036854775807,min(max(limit,1),100)))
            rows.reverse()
            first=rows[0]['sequence'] if rows else before
            return {'events':rows,'next_after':rows[-1]['sequence'] if rows else after,'before':first,
                'has_older':bool(rows and self.rows('SELECT 1 FROM events WHERE conversation_id=? AND sequence<? LIMIT 1',(conversation,first)))}
        rows=self.rows('SELECT * FROM events WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?',(conversation,max(0,after),min(max(limit,1),100)))
        return {'events':rows,'next_after':rows[-1]['sequence'] if rows else after}
    def credential_reference(self,user,worker):
        w=self.one('SELECT * FROM workers WHERE id=?',(worker,)); self.manage(user,w)
        return {'worker_id':worker,'credential_ref':w['credential_ref']}  # backend-only; never exposed by HTTP
    def execution_result(self,user,task_id):
        task=self.task(user,task_id)
        return {'task':task,'runs':self.rows('SELECT * FROM execution_runs WHERE task_id=? ORDER BY rowid',(task_id,))}
    def artifact(self,user,artifact):
        matches=self.rows("SELECT project_id FROM events WHERE json_extract(metadata,'$.artifact_id')=?",(artifact,))
        if not matches: raise ValueError('Artifact not found')
        self.access(user,matches[0]['project_id'])
        if not artifact.isalnum(): raise ValueError('Invalid artifact ID')
        return {'content':(self.root/'artifacts'/artifact).read_text()}
    def mutate(self,user,method,data):
        reject_credentials(data)
        with self.lock:
            if self.closing: raise ValueError('Relay is shutting down; retry after restart')
            self.user(user)
            self.authorize_mutation(user,method,data)
            key=user+':'+str(data.get('request_id',uid()))
            fingerprint=hashlib.sha256(json.dumps({'method':method,'data':{k:v for k,v in data.items() if k!='request_id'}},sort_keys=True).encode()).hexdigest()
            # Cache only IDs/public results. Permission is checked again before reads.
            prior=self.rows('SELECT result FROM requests WHERE key=?',(key,))
            if prior:
                signature=self.rows('SELECT fingerprint FROM request_signatures WHERE key=?',(key,))
                if not signature or signature[0]['fingerprint']!=fingerprint: raise ValueError('request_id was already used for a different operation; use a new request_id')
                cached=json.loads(prior[0]['result'])
                if isinstance(cached,dict) and cached.get('conversation_id'):
                    self.access(user,self.project(cached))
                if (method.startswith('workers/') and method!='workers/stop') or method=='availability':
                    self.manage(user,self.one('SELECT * FROM workers WHERE id=?',(data.get('worker_id') or cached['id'],)))
                return cached
            if method=='projects': result=self.create_project(user,data['name'])
            elif method=='prompt': result=self.submit(user,data['conversation_id'],data['prompt'],data.get('worker_id'),data.get('execution_mode'),data.get('pause_after_turn',False),data.get('native_phases',2))
            elif method in ('switch','limit'):
                result=self.switch(user,data['task_id'],int(data['generation']),'Simulated usage limit' if method=='limit' else 'Manual worker switch',data.get('target_worker_id'),'simulated_limit' if method=='limit' else data.get('reason_code','manual_switch'))
            elif method=='continue': result=self.continue_task(user,self.task(user,data['task_id']))
            elif method=='pause':result=self.switch(user,data['task_id'],int(data['generation']),'Pause and checkpoint',None,'task_pause')
            elif method=='resume':result=self.resume_task(user,self.task(user,data['task_id']),int(data['generation']))
            elif method=='cancel': result=self.cancel(user,self.task(user,data['task_id']))
            elif method=='availability': result=self.configure(user,data)
            elif method=='workers/enroll': result=self.enroll(user,data)
            elif method=='workers/configure': result=self.configure(user,data)
            elif method=='workers/revoke': result=self.configure(user,data,True)
            elif method=='workers/remove': result=self.management.remove(user,data)
            elif method=='workers/stop': result=self.management.stop(user,data)
            elif method=='tasks/assign': result=self.management.assign(user,data)
            elif method=='tasks/start': result=self.management.start(user,data['task_id'])
            elif method=='membership':
                result=self.set_membership(user,data)
            elif method=='demo':
                self.access(user,data['project_id']); c=self.one('SELECT * FROM conversations WHERE project_id=?',(data['project_id'],))
                t=self.submit(user,c['id'],'Add a password-strength validator and tests to this sample project.','codexA')
                result=self.switch(user,t['id'],t['generation'],'Simulated usage limit','codexB','simulated_limit')
            else: raise ValueError('Unknown action')
            self.db.execute('INSERT INTO requests VALUES(?,?)',(key,json.dumps(result)))
            self.db.execute('INSERT INTO request_signatures VALUES(?,?)',(key,fingerprint))
            return result
    def close(self):
        with self.lock:
            self.closing=True
            jobs=list(self.jobs.values())
            health_jobs=list(self.health_jobs.values())
            for task_id,job in list(self.jobs.items()): job['cancel'].set(); job['adapter'].cancel(task_id)
        for job in jobs:
            if job['thread']: job['thread'].join(timeout=15)
        for job in health_jobs:job.join(timeout=5)
        if any(job['thread'] and job['thread'].is_alive() for job in jobs):
            raise RuntimeError('A task is still stopping. The workspace lock must remain held until shutdown completes.')
        self.db.close()
