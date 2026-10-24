"""Invite-only Relay identities. Never handles Codex/provider credentials.

One trusted organization per deployment. Authentication secrets have separate
hashed tables; none are exposed by project, event, export or worker APIs.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .security import clean

class LoginRequired(PermissionError):
    status=401

class RateLimited(ValueError):
    status=429

_hash_slots=threading.BoundedSemaphore(2)

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def csrf_token(token):return hmac.new(token.encode(),b'relay-csrf-v1',hashlib.sha256).hexdigest()
def email_address(value):
    if not isinstance(value,str) or len(value)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):raise ValueError('Enter a valid Relay email address')
    return value.strip().lower()

def hash_password(password,salt=None):
    if not isinstance(password,str) or not 12<=len(password)<=128 or len(password.encode())>512:
        raise ValueError('Use a Relay password of 12–128 characters')
    if not _hash_slots.acquire(timeout=1):raise RateLimited('Sign-in is busy; try again shortly')
    try:
        salt=salt or secrets.token_bytes(16)
        key=hashlib.scrypt(password.encode(),salt=salt,n=2**17,r=8,p=1,maxmem=256*1024*1024,dklen=32)
        return 'scrypt$131072$8$1$'+salt.hex()+'$'+key.hex()
    finally:_hash_slots.release()

def verify_password(password,stored):
    try:
        _,n,r,p,salt,key=stored.split('$')
        if (n,r,p)!=('131072','8','1'):return False
        actual=hash_password(password,bytes.fromhex(salt))
        return hmac.compare_digest(actual,stored)
    except (ValueError,TypeError):return False

class InviteIdentityProvider:
    mode='invite'
    production_ready=True  # Requires the HTTPS/loopback boundary in server.py.
    def __init__(self,relay,ttl=28800,idle=1800,clock=None,owner_id=None):
        self.relay=relay;self.ttl=ttl;self.idle=idle;self.clock=clock or time.time
        self.owner_id=owner_id
        self._dummy=hash_password(secrets.token_urlsafe(24))
    def limit(self,kind,key,maximum=10,window=600):
        current=self.clock();bucket=kind+':'+digest(str(key))
        with self.relay.lock:
            db=self.relay.db
            db.execute('DELETE FROM auth_limits WHERE started_at<?',(current-86400,))
            row=db.execute('SELECT started_at,attempts FROM auth_limits WHERE bucket=?',(bucket,)).fetchone()
            if row and current-row[0]<window:
                if row[1]>=maximum:raise RateLimited('Too many attempts; wait and try again')
                db.execute('UPDATE auth_limits SET attempts=attempts+1 WHERE bucket=?',(bucket,))
            else:db.execute('INSERT OR REPLACE INTO auth_limits VALUES(?,?,1)',(bucket,current))
    def create_account(self,email,name,password,role):
        email=email_address(email)
        if not isinstance(name,str) or not name.strip() or len(name)>100:raise ValueError('Display name must contain 1–100 characters')
        user=uuid.uuid4().hex
        self.relay.db.execute('INSERT INTO users VALUES(?,?,?)',(user,clean(name.strip()),role))
        self.relay.db.execute('INSERT INTO accounts VALUES(?,?,?,0,?)',(user,email,password,self.clock()))
        self.relay.db.execute('INSERT INTO organization_memberships VALUES(?,?,?)',('orgdemo',user,role))
        return user
    def bootstrap(self,email,name,password,local_owner=False):
        encoded=hash_password(password)
        with self.relay.lock:
            db=self.relay.db;db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('SELECT 1 FROM accounts').fetchone():raise ValueError('Accounts already exist; bootstrap is disabled')
                user=self.create_account(email,name,encoded,'admin')
                if local_owner:
                    from .fs_lock import process_uid
                    db.execute('INSERT INTO local_owner_binding VALUES(1,?,?,?)',(user,process_uid(),self.clock()))
                self.relay.audit(user,'account_bootstrap',metadata={'local_owner':local_owner})
                db.execute('COMMIT');return {'id':user,'name':name}
            except Exception:db.execute('ROLLBACK');raise
    def login(self,email,password,client='local',previous=None):
        email=email_address(email)
        self.limit('login-ip',client,50);self.limit('login-account',email,10)
        with self.relay.lock:
            rows=self.relay.rows('SELECT * FROM accounts WHERE email=?',(email,));account=rows[0] if rows else None
        valid=verify_password(password,account['password_hash'] if account else self._dummy)
        with self.relay.lock:
            if not account or not valid or account['disabled'] or (self.owner_id and account['user_id']!=self.owner_id):
                self.relay.audit(None,'login_failed',metadata={'client_digest':digest(client)})
                raise LoginRequired('Email or password is incorrect')
            # Recheck disable/revocation decisions made while scrypt ran.
            if self.relay.one('SELECT disabled FROM accounts WHERE user_id=?',(account['user_id'],))['disabled']:raise LoginRequired('Email or password is incorrect')
            if previous:self.logout(previous)
            token=secrets.token_urlsafe(32);now=self.clock()
            self.relay.db.execute('DELETE FROM sessions WHERE expires_at<? OR last_seen<?',(now,now-self.idle))
            self.relay.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(digest(token),account['user_id'],now,now+self.ttl,now))
            self.relay.audit(account['user_id'],'login')
            return token
    def resolve(self,token):
        with self.relay.lock:
            rows=self.relay.rows('SELECT s.*,a.disabled FROM sessions s JOIN accounts a ON a.user_id=s.user_id WHERE s.digest=?',(digest(token),))
            now=self.clock()
            if not rows or rows[0]['disabled'] or rows[0]['expires_at']<=now or rows[0]['last_seen']+self.idle<=now or (self.owner_id and rows[0]['user_id']!=self.owner_id):
                self.relay.db.execute('DELETE FROM sessions WHERE digest=?',(digest(token),));raise LoginRequired('Sign in to Relay')
            row=rows[0];self.relay.org_member(row['user_id'])
            self.relay.db.execute('UPDATE sessions SET last_seen=? WHERE digest=?',(now,row['digest']))
            return row['user_id']
    def logout(self,token):
        with self.relay.lock:self.relay.db.execute('DELETE FROM sessions WHERE digest=?',(digest(token),))
    def revoke_sessions(self,actor,target=None):
        target=target or actor
        with self.relay.lock:
            if actor!=target and not self.relay.is_admin(actor):raise PermissionError('Administrator required')
            self.relay.org_member(target)
            self.relay.db.execute('DELETE FROM sessions WHERE user_id=?',(target,));self.relay.audit(actor,'sessions_revoked',metadata={'member':target})
        return {'ok':True}
    def invite(self,actor,data):
        if self.owner_id:raise PermissionError('Local-owner mode does not invite or delegate to other Relay users')
        with self.relay.lock:
            if not self.relay.is_admin(actor):raise PermissionError('Administrator required')
            email=email_address(data.get('email'));org_role=data.get('org_role','member');project_role=data.get('project_role','member');project=data.get('project_id') or None
            if org_role not in ('admin','member','viewer') or project_role not in ('admin','member','viewer'):raise ValueError('Unknown role')
            if project:self.relay.access(actor,project)
            token=secrets.token_urlsafe(32);identifier=uuid.uuid4().hex
            self.relay.db.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',(identifier,digest(token),email,org_role,project,project_role,actor,self.clock()+86400))
            self.relay.audit(actor,'invitation_created',project=project,metadata={'invitation_id':identifier,'role':org_role})
            return {'id':identifier,'invitation_token':token,'expires_in':86400}
    def revoke_invite(self,actor,identifier):
        with self.relay.lock:
            if not self.relay.is_admin(actor):raise PermissionError('Administrator required')
            self.relay.db.execute('UPDATE invitations SET revoked_at=? WHERE id=?',(self.clock(),identifier));self.relay.audit(actor,'invitation_revoked',metadata={'invitation_id':identifier})
        return {'ok':True}
    def accept(self,data,actor=None,client='local'):
        if self.owner_id:raise PermissionError('Local-owner accounts must be provisioned offline; registration is disabled')
        self.limit('invite-ip',client,20)
        email=email_address(data.get('email'))
        token=data.get('invitation_token','')
        if not isinstance(token,str) or len(token)>200:raise ValueError('Invalid Relay invitation')
        encoded=hash_password(data.get('password')) if actor is None else None
        with self.relay.lock:
            db=self.relay.db;db.execute('BEGIN IMMEDIATE')
            try:
                rows=self.relay.rows('SELECT * FROM invitations WHERE digest=?',(digest(token),))
                if not rows or rows[0]['email']!=email or rows[0]['accepted_at'] or rows[0]['revoked_at'] or rows[0]['expires_at']<=self.clock():raise ValueError('Invitation is invalid, expired or already used')
                invitation=rows[0]
                if actor:
                    if self.relay.one('SELECT email FROM accounts WHERE user_id=?',(actor,))['email']!=email:raise PermissionError('Invitation belongs to another Relay account')
                    user=actor
                else:user=self.create_account(email,data.get('name'),encoded,invitation['org_role'])
                # Invitations never downgrade an existing organization role.
                if invitation['project_id']:db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(user,invitation['project_id'],invitation['project_role']))
                db.execute('UPDATE invitations SET accepted_at=? WHERE id=?',(self.clock(),invitation['id']))
                self.relay.audit(user,'invitation_accepted',project=invitation['project_id'],metadata={'invitation_id':invitation['id'],'invited_by':invitation['created_by']})
                db.execute('COMMIT');return {'ok':True,'id':user}
            except sqlite3.IntegrityError:db.execute('ROLLBACK');raise ValueError('Account already exists; sign in and accept the invitation') from None
            except Exception:db.execute('ROLLBACK');raise
