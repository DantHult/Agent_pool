"""Versioned, additive migrations from the original Relay demo schema."""
from datetime import datetime, timezone

BASE = '''
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT,role TEXT);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT,organization_id TEXT);
CREATE TABLE IF NOT EXISTS memberships(user_id TEXT,project_id TEXT,role TEXT,PRIMARY KEY(user_id,project_id));
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,project_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,name TEXT,state TEXT,owner_id TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,conversation_id TEXT,state TEXT,worker_id TEXT,objective TEXT,generation INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(conversation_id) WHERE state NOT IN ('completed','failed','cancelled');
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,organization_id TEXT,project_id TEXT,conversation_id TEXT,task_id TEXT,sequence INTEGER,timestamp TEXT,worker_id TEXT,event_type TEXT,role TEXT,content TEXT,metadata TEXT,UNIQUE(conversation_id,sequence));
CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY,task_id TEXT,version INTEGER,data TEXT);
CREATE TABLE IF NOT EXISTS handoffs(id TEXT PRIMARY KEY,task_id TEXT,source TEXT,target TEXT,reason TEXT,checkpoint TEXT,context_version INTEGER,timestamp TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,task_id TEXT,worker_id TEXT,session_id TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY,result TEXT);
'''

EXTRA = '''
CREATE TABLE IF NOT EXISTS organizations(id TEXT PRIMARY KEY,name TEXT NOT NULL,is_demo INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS organization_memberships(organization_id TEXT,user_id TEXT,role TEXT,PRIMARY KEY(organization_id,user_id));
CREATE TABLE IF NOT EXISTS worker_projects(worker_id TEXT,project_id TEXT,authorized_by TEXT,created_at TEXT,PRIMARY KEY(worker_id,project_id));
CREATE TABLE IF NOT EXISTS capacity_sources(worker_id TEXT PRIMARY KEY,quota_scope TEXT NOT NULL,quota_owner TEXT NOT NULL,availability TEXT NOT NULL,limit_kind TEXT,reset_at TEXT,observed_at TEXT,source TEXT NOT NULL,value REAL,confidence TEXT);
CREATE TABLE IF NOT EXISTS workspace_checkpoints(id TEXT PRIMARY KEY,project_id TEXT,conversation_id TEXT,task_id TEXT,worker_id TEXT,git_commit TEXT,created_at TEXT,reason TEXT,uncommitted_changes TEXT);
CREATE TABLE IF NOT EXISTS workspace_leases(project_id TEXT PRIMARY KEY,task_id TEXT UNIQUE,worker_id TEXT,token TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY,organization_id TEXT,user_id TEXT,action TEXT,worker_id TEXT,project_id TEXT,timestamp TEXT,metadata TEXT);
CREATE VIEW IF NOT EXISTS project_memberships AS SELECT * FROM memberships;
CREATE VIEW IF NOT EXISTS messages AS SELECT * FROM events WHERE role IN ('user','assistant');
CREATE VIEW IF NOT EXISTS tool_events AS SELECT * FROM events WHERE event_type IN ('tool_invocation','tool_output');
CREATE VIEW IF NOT EXISTS context_summaries AS SELECT * FROM contexts;
CREATE VIEW IF NOT EXISTS task_assignments AS SELECT * FROM assignments;
'''


def migrate(db):
    db.executescript('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;'+BASE)
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS request_signatures(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)')
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
        migrate_live(db)
        migrate_management(db)
        migrate_reservation_leases(db)
        migrate_project_deletion(db)
        migrate_project_internet(db)
        return
    timestamp=datetime.now(timezone.utc).isoformat()
    db.execute('BEGIN IMMEDIATE')
    try:
        additions={
            'workers':{'organization_id':"TEXT NOT NULL DEFAULT 'orgdemo'",'owner_type':"TEXT NOT NULL DEFAULT 'user'",'backend':"TEXT NOT NULL DEFAULT 'codex'",'execution_mode':"TEXT NOT NULL DEFAULT 'simulation'",'opt_in':'INTEGER NOT NULL DEFAULT 1','max_parallel_tasks':'INTEGER NOT NULL DEFAULT 1','created_at':'TEXT','updated_at':'TEXT','credential_ref':'TEXT'},
            'tasks':{'requested_by':'TEXT','execution_mode':"TEXT NOT NULL DEFAULT 'simulation'",'last_error':'TEXT','pending_action':'TEXT','pending_target':'TEXT','pending_reason':'TEXT','pending_authorized_by':'TEXT'},
            'handoffs':{'reason_code':"TEXT NOT NULL DEFAULT 'manual_switch'",'authorized_by':'TEXT','result':'TEXT','error':'TEXT'},
            'assignments':{'authorized_by':'TEXT','native_thread_id':'TEXT','context_version':'INTEGER','status':"TEXT NOT NULL DEFAULT 'released'",'finished_at':'TEXT'},
        }
        for table,columns in additions.items():
            existing={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            for name,kind in columns.items():
                if name not in existing: db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
        # Execute individual statements to keep the migration atomic (executescript commits).
        for statement in EXTRA.split(';'):
            if statement.strip(): db.execute(statement)
        # Triggers contain semicolons; install them outside the splitter.
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS audit_no_{action.lower()} BEFORE {action} ON audit_events BEGIN SELECT RAISE(ABORT,'append only'); END")
        db.execute("INSERT OR IGNORE INTO organizations VALUES('orgdemo','Relay Labs (demo)',1)")
        for user in db.execute('SELECT id,role FROM users').fetchall():
            db.execute('INSERT OR IGNORE INTO organization_memberships VALUES(?,?,?)',('orgdemo',user[0],'admin' if user[1]=='admin' else 'member'))
        for w in db.execute('SELECT id,owner_id,state FROM workers').fetchall():
            db.execute('UPDATE workers SET created_at=COALESCE(created_at,?),updated_at=COALESCE(updated_at,?) WHERE id=?',(timestamp,timestamp,w[0]))
            db.execute('INSERT OR IGNORE INTO capacity_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(w[0],'simulation:'+w[0],w[1],w[2],'simulated',None,timestamp,'simulated',None,'unknown'))
            # One explicit demo grant; same-org membership never grants all projects.
            if db.execute("SELECT 1 FROM projects WHERE id='demo'").fetchone():
                db.execute('INSERT OR IGNORE INTO worker_projects VALUES(?,?,?,?)',(w[0],'demo',w[1],timestamp))
            for project in db.execute("SELECT project_id FROM memberships WHERE user_id=? AND project_id!='demo'",(w[1],)).fetchall():
                db.execute('INSERT OR IGNORE INTO worker_projects VALUES(?,?,?,?)',(w[0],project[0],w[1],timestamp))
        db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(timestamp,))
        db.execute('INSERT INTO schema_migrations VALUES(2,?)',(timestamp,))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK'); raise
    migrate_live(db)
    migrate_management(db)
    migrate_reservation_leases(db)
    migrate_project_deletion(db)
    migrate_project_internet(db)


def migrate_live(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
        migrate_native(db); return
    db.execute('BEGIN IMMEDIATE')
    try:
        columns={r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        for name,kind in {'pause_after_turn':'INTEGER NOT NULL DEFAULT 0','execution_phase':'INTEGER NOT NULL DEFAULT 0','progress_json':"TEXT NOT NULL DEFAULT '{}'"}.items():
            if name not in columns: db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {kind}')
        db.execute("""CREATE TABLE IF NOT EXISTS execution_runs(
            id TEXT PRIMARY KEY,task_id TEXT,assignment_id TEXT,worker_id TEXT,execution_mode TEXT,
            state TEXT,started_at TEXT,finished_at TEXT,exit_code INTEGER,duration_ms INTEGER,
            native_thread_id TEXT,error_category TEXT,retry_after_seconds REAL,summary TEXT,progress_json TEXT)""")
        db.execute('INSERT INTO schema_migrations VALUES(3,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK'); raise
    migrate_native(db)


def migrate_native(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
        migrate_accounts(db);return
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('ALTER TABLE workers ADD COLUMN native_profile TEXT')
        db.execute('ALTER TABLE tasks ADD COLUMN native_phases INTEGER NOT NULL DEFAULT 2')
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_native_profile ON workers(native_profile) WHERE execution_mode='native'")
        db.execute('INSERT INTO schema_migrations VALUES(4,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise
    migrate_accounts(db)


def migrate_accounts(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=5').fetchone():
        migrate_local_owner(db);return
    db.execute('BEGIN IMMEDIATE')
    try:
        statements=[
            'CREATE TABLE accounts(user_id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL)',
            'CREATE TABLE sessions(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,last_seen REAL NOT NULL)',
            'CREATE INDEX sessions_user ON sessions(user_id)',
            'CREATE TABLE invitations(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,email TEXT NOT NULL,org_role TEXT NOT NULL,project_id TEXT,project_role TEXT NOT NULL,created_by TEXT NOT NULL,expires_at REAL NOT NULL,accepted_at REAL,revoked_at REAL)',
            'CREATE TABLE auth_limits(bucket TEXT PRIMARY KEY,started_at REAL NOT NULL,attempts INTEGER NOT NULL)',
        ]
        for statement in statements:db.execute(statement)
        db.execute('INSERT INTO schema_migrations VALUES(5,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise
    migrate_local_owner(db)


def migrate_local_owner(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=6').fetchone():
        migrate_ide(db);return
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('CREATE TABLE local_owner_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),user_id TEXT NOT NULL,os_uid INTEGER NOT NULL,created_at REAL NOT NULL)')
        db.execute('INSERT INTO schema_migrations VALUES(6,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise
    migrate_ide(db)


def migrate_ide(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=7').fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        # Preserve every existing ID. Conversations share the project's one
        # authoritative workspace; execution remains serialized per project.
        db.execute('ALTER TABLE conversations RENAME TO conversations_v6')
        db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,title TEXT NOT NULL DEFAULT 'New conversation',created_at TEXT,updated_at TEXT)")
        db.execute("INSERT INTO conversations(id,project_id,title,created_at,updated_at) SELECT c.id,c.project_id,COALESCE(p.name,'Conversation'),COALESCE((SELECT MIN(timestamp) FROM events e WHERE e.conversation_id=c.id),?),COALESCE((SELECT MAX(timestamp) FROM events e WHERE e.conversation_id=c.id),?) FROM conversations_v6 c LEFT JOIN projects p ON p.id=c.project_id",(datetime.now(timezone.utc).isoformat(),)*2)
        db.execute('DROP TABLE conversations_v6')
        db.execute('CREATE INDEX conversations_project ON conversations(project_id)')
        db.execute('CREATE TABLE human_workspace_locks(project_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,client_id TEXT NOT NULL,session_hash TEXT NOT NULL,created_at TEXT NOT NULL)')
        db.execute('CREATE TABLE editor_drafts(project_id TEXT,user_id TEXT,path TEXT,base_version TEXT,content TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project_id,user_id,path))')
        db.execute('INSERT INTO schema_migrations VALUES(7,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise


def migrate_management(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('ALTER TABLE conversations ADD COLUMN archived_at TEXT')
        db.execute('ALTER TABLE workers ADD COLUMN removed_at TEXT')
        db.execute('ALTER TABLE tasks ADD COLUMN requested_worker_id TEXT')
        db.execute("UPDATE tasks SET requested_worker_id=COALESCE(worker_id,(SELECT json_extract(e.metadata,'$.requested_worker_id') FROM events e WHERE e.task_id=tasks.id AND e.event_type='queue' AND json_valid(e.metadata) AND json_extract(e.metadata,'$.requested_worker_id') IS NOT NULL ORDER BY e.sequence DESC LIMIT 1))")
        db.execute('DROP INDEX one_native_profile')
        db.execute("CREATE UNIQUE INDEX one_native_profile ON workers(native_profile) WHERE execution_mode='native' AND removed_at IS NULL")
        db.execute('INSERT INTO schema_migrations VALUES(8,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise


def migrate_reservation_leases(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=9').fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        # Existing reservations keep their owner; a NULL heartbeat is measured
        # from process start, so a live browser can reconnect after upgrade.
        db.execute('ALTER TABLE human_workspace_locks ADD COLUMN heartbeat_at REAL')
        db.execute('ALTER TABLE human_workspace_locks ADD COLUMN label TEXT')
        db.execute('INSERT INTO schema_migrations VALUES(9,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise


def migrate_project_deletion(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=10').fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        # A deleted project keeps its row so append-only events and audit
        # entries still name a real project; Relay refuses all access to it.
        db.execute('ALTER TABLE projects ADD COLUMN deleted_at TEXT')
        db.execute('INSERT INTO schema_migrations VALUES(10,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise


def migrate_project_internet(db):
    if db.execute('SELECT 1 FROM schema_migrations WHERE version=11').fetchone():return
    db.execute('BEGIN IMMEDIATE')
    try:
        # Whether a project's agents may reach any public host (the default
        # without a row), only listed domains, or nothing; and the names
        # (never values) of API keys from Relay's environment for listed domains.
        db.execute("CREATE TABLE project_internet(project_id TEXT PRIMARY KEY,mode TEXT NOT NULL DEFAULT 'public',domains TEXT NOT NULL,key_names TEXT NOT NULL,updated_by TEXT NOT NULL,updated_at TEXT NOT NULL)")
        db.execute('INSERT INTO schema_migrations VALUES(11,?)',(datetime.now(timezone.utc).isoformat(),))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK');raise
