"""Identity boundary: a future verified OIDC implementation replaces this provider."""
import secrets
from typing import Protocol

class IdentityProvider(Protocol):
    def login(self, user_id: str) -> str: ...
    def resolve(self, session: str) -> str: ...
    def logout(self, session: str) -> None: ...

class LocalDemoIdentityProvider:
    mode='local_demo'
    production_ready=False
    def __init__(self,lookup): self.lookup=lookup; self.sessions={}
    def login(self,user_id):
        self.lookup(user_id)
        session=secrets.token_urlsafe(32); self.sessions[session]=user_id
        return session
    def resolve(self,session):
        if session not in self.sessions: raise PermissionError('Sign in to the local demo')
        return self.sessions[session]
    def logout(self,session): self.sessions.pop(session,None)
