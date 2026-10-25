"""Application redaction and credential-input rejection shared by HTTP and storage."""
import os
import re

FORBIDDEN_FIELDS={'credential_ref','api_key','openai_api_key','access_token','refresh_token','id_token','session_token','cookie','cookies','password','auth_dir','auth_json','chatgpt_token','cli_auth','secret','token','authorization','credentials','chatgpt_credentials','codex_auth','auth_directory','session_cookie','cookie_header','client_secret'}

SECRET_PREFIX='RELAY_SECRET_'

def clean(value):
    value=str(value)
    for name in ('RELAY_ORG_OPENAI_API_KEY','RELAY_CODEX_PROXY_TOKEN','OPENAI_API_KEY','CODEX_API_KEY'):
        secret=os.environ.get(name)
        if secret: value=value.replace(secret,'[REDACTED]')
    # Project API keys from Relay's environment never appear in visible output.
    for name,secret in os.environ.items():
        if name.startswith(SECRET_PREFIX) and len(secret)>=6: value=value.replace(secret,'[REDACTED]')
    # Preserve valid JSON when credential values appear in visible diagnostics.
    value=re.sub(r'(?i)("(?:access_token|refresh_token|id_token|api_key|session_token|cookie)"\s*:\s*)"(?:\\.|[^"\\])*"',r'\1"[REDACTED]"',value)
    value=re.sub(r'(?i)(bearer\s+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*)\S+',r'\1[REDACTED]',value)
    return re.sub(r'sk-[A-Za-z0-9_-]{8,}','[REDACTED]',value)

def scrub(value):
    if isinstance(value,dict): return {k:scrub(v) for k,v in value.items() if k.lower() not in FORBIDDEN_FIELDS}
    if isinstance(value,list): return [scrub(v) for v in value]
    return clean(value) if isinstance(value,str) else value

def reject_credentials(data):
    if isinstance(data,dict):
        for key,value in data.items():
            if key.lower() in FORBIDDEN_FIELDS: raise ValueError('Credential fields are not accepted. Authenticate native profiles locally with Codex login or configure optional organization execution secrets on the server.')
            reject_credentials(value)
    elif isinstance(data,list):
        for value in data: reject_credentials(value)
