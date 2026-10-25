"""Typed development commands: canonical Git or the existing isolated runner.

No shell interpolation, network capability or host Git credentials are granted.
"""
from pathlib import PurePosixPath
import shlex
import time

from .console import COMMANDS
from .ide import file_path


def parse(command, cwd=''):
    if not isinstance(command,str) or not command.strip() or len(command)>4000 or '\0' in command:
        raise ValueError('Enter a development command of at most 4,000 characters')
    try:argv=shlex.split(command)
    except ValueError:raise ValueError('Unclosed command quote; use matching single or double quotes') from None
    if not argv or argv[0] not in COMMANDS:raise ValueError('Supported tools: python, python3, node, npm, npx, pytest and structured git. Host shells and arbitrary executables are unavailable.')
    if any(value in ('|','||','&&',';','>','>>','<','2>','&') for value in argv):raise ValueError('Run one argv command at a time; shell operators are unavailable')
    if cwd:file_path(cwd,write=True)
    if argv[0]!='git':return {'kind':'sandbox','command':command,'cwd':cwd}
    args=argv[1:]
    if not args:raise ValueError('Enter a supported Git subcommand')
    sub,*values=args
    if sub=='push':raise ValueError('Remote Git pushes are unavailable. Relay grants no network or host Git credentials; use your separately authorized Git client outside Relay.')
    if sub=='restore' and values[:1]==['--staged']:
        sub,values='unstage',values[1:]
    allowed=(sub=='status' and values in ([],['--short'])) or (sub=='diff' and values in ([],['--staged'])) or (sub=='log' and not values) or (sub=='branch' and len(values)<=1) or (sub=='switch' and (len(values)==1 or len(values)==2 and values[0]=='-c')) or (sub=='commit' and len(values)==2 and values[0]=='-m') or (sub in ('add','unstage') and bool(values))
    if not allowed:raise ValueError('Supported Git: status, diff [--staged], add PATH…, restore --staged PATH…, commit -m "message", log, branch [NAME], switch [-c] NAME. Arbitrary flags and remote operations are unavailable.')
    if sub=='status':values=[]
    if sub in ('add','unstage'):
        mapped=[]
        for name in values:
            if name=='.':mapped.append(cwd or '.')
            else:
                file_path(name,write=True)
                mapped.append(str(PurePosixPath(cwd)/name) if cwd else name)
        values=mapped
    return {'kind':'git','arguments':[sub,*values],'command':command,'cwd':cwd}


class DevelopmentTerminal:
    def __init__(self,server):self.server=server;self.relay=server.relay;self.ide=server.ide

    def _authorize(self,user,data,binding):
        if not self.ide.console:raise PermissionError('The development terminal requires the authenticated local owner')
        self.ide.console._valid(user,data['project_id'],binding)
        self.ide.conversation(user,data['project_id'],data.get('conversation_id'))

    def dispatch(self,user,data,binding):
        with self.relay.lock:
            self._authorize(user,data,binding)
            plan=parse(data.get('command'),data.get('cwd',''))
            try:
                with self.relay.ws._project_directory(data['project_id']) as root:
                    with self.relay.ws._directory(root,file_path(plan['cwd']) if plan['cwd'] else []):pass
            except OSError:raise ValueError('Command working directory must be an existing project folder without symlinks') from None
            if plan['kind']=='sandbox':return plan
            args=plan['arguments'];started=time.monotonic()
            writing=args[0] in ('add','unstage','commit','switch') or args[0]=='branch' and len(args)>1
            if writing:self.ide.prepare(user,data['project_id'],data['client_id'],binding)
            result=self.ide.git(user,data['project_id'],data['client_id'],binding,args,data.get('conversation_id'))
            return {**plan,**result,'duration_ms':int((time.monotonic()-started)*1000),'scope':'authoritative project Git'}

    def file(self,user,data,binding):
        with self.relay.lock:
            self._authorize(user,data,binding)
            path=data.get('path');parts=file_path(path,write=True)
            suffix=PurePosixPath(path).suffix.lower()
            runtime='python3' if suffix=='.py' else 'node' if suffix in ('.js','.mjs','.cjs') else None
            if not runtime:raise ValueError('Run File supports Python (.py) and Node JavaScript (.js, .mjs, .cjs). Use an approved project script for other file types.')
            try:self.ide._read(data['project_id'],path)
            except OSError:raise ValueError('Run File requires an accessible regular project file; symlinks and hard links are blocked') from None
            return {'kind':'sandbox','command':shlex.join([runtime,parts[-1]]),'cwd':'/'.join(parts[:-1]),'path':path}
