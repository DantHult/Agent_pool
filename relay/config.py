"""Explicit local environment-file loading; never execute shell or echo values."""
import os
from pathlib import Path
import re
import shlex

ENV_KEYS={
    'RELAY_DATA_DIR','RELAY_AUTH_MODE','RELAY_ENABLE_ORG_API','RELAY_ORG_OPENAI_API_KEY',
    'RELAY_CODEX_PROXY_TOKEN','RELAY_CODEX_IMAGE','RELAY_CODEX_NETWORK',
    'RELAY_CODEX_PROXY_URL','RELAY_CODEX_MODEL','RELAY_CODEX_TIMEOUT_SECONDS',
    'RELAY_CODEX_REQUEST_RETRIES','RELAY_CODEX_STREAM_RETRIES','RELAY_RUN_LIVE_SMOKE',
}

def load_env_file(path: str | Path) -> None:
    """Load only Relay settings. Existing shell exports take precedence."""
    file=Path(path)
    if not file.is_file(): raise ValueError('Environment file not found; copy .env.example to .env and configure it locally')
    if file.stat().st_size>65536: raise ValueError('Environment file exceeds the size limit')
    try: lines=file.read_text().splitlines()
    except UnicodeError: raise ValueError('Environment file must be UTF-8 text') from None
    values={}
    for number,line in enumerate(lines,1):
        line=line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line=line[7:].lstrip()
        key,separator,value=line.partition('=');key=key.strip()
        if not separator or not re.fullmatch(r'RELAY_[A-Z0-9_]+',key) or key not in ENV_KEYS:
            raise ValueError(f'Unsupported environment setting at line {number}; use .env.example')
        try: parts=shlex.split(value,comments=True,posix=True)
        except ValueError: raise ValueError(f'Invalid quoting in environment file at line {number}') from None
        if len(parts)>1: raise ValueError(f'Quote values containing spaces in environment file at line {number}')
        values[key]=parts[0] if parts else ''
    # Commit only after the entire file validates; malformed files cannot partly enable execution.
    for key,value in values.items(): os.environ.setdefault(key,value)
