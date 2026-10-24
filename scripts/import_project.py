"""Trusted offline operator imports committed files, never Git/auth metadata."""
import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.fs_lock import null_device, which_git  # noqa: E402
from relay.store import Relay  # noqa: E402
from scripts.demo import exclusive_demo_directory  # noqa: E402

EXCLUDED={'.git','.relay','.codex','.aws','.ssh','auth.json','credentials','id_rsa','id_ed25519'}

def import_project(relay,actor,repository,name):
    if not relay.is_admin(actor):raise PermissionError('Organization administrator required')
    repository=Path(repository).resolve(strict=True)
    git_bin=which_git() or 'git'
    null=null_device()
    def git(*args):return subprocess.check_output([git_bin,'-c','safe.directory=*','-c',f'core.hooksPath={null}','-c','core.fsmonitor=false','-C',str(repository),*args],stderr=subprocess.DEVNULL,timeout=60)
    listing=git('ls-tree','-rz','HEAD');files=[];total=0
    for record in listing.split(b'\0'):
        if not record:continue
        metadata,filename=record.split(b'\t',1);mode,kind,blob=metadata.split();path=Path(filename.decode('utf-8'))
        if path.is_absolute() or any(part in ('..','.') for part in path.parts):raise ValueError('Unsafe repository path')
        if any(part in EXCLUDED or part=='.env' or (part.startswith('.env.') and part!='.env.example') for part in path.parts):continue
        if kind!=b'blob' or mode not in (b'100644',b'100755'):raise ValueError('Symlinks and submodules are not accepted in managed imports')
        size=int(git('cat-file','-s',blob.decode()).strip());total+=size
        if size>relay.ws.MAX_FILE_BYTES or total>relay.ws.MAX_CHECKPOINT_BYTES or len(files)>=relay.ws.MAX_CHECKPOINT_FILES:raise ValueError('Repository exceeds managed snapshot limits')
        files.append((path,git('cat-file','blob',blob.decode())))
    project=relay.create_project(actor,name)['id'];target=relay.ws.path(project)
    for path,content in files:
        output=target/path;output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(content)
    checkpoint=relay.ws.checkpoint(project,'Import authorized repository snapshot')
    relay.audit(actor,'repository_imported',project=project,metadata={'checkpoint':checkpoint,'files':len(files)})
    return {'project_id':project,'checkpoint':checkpoint,'files':len(files)}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',default='shared-data');parser.add_argument('--email',required=True);parser.add_argument('--repo',required=True);parser.add_argument('--name',required=True);args=parser.parse_args()
    with exclusive_demo_directory(args.data):
        relay=Relay(args.data,demo=False)
        try:
            actor=relay.one('SELECT user_id FROM accounts WHERE email=? AND disabled=0',(args.email.strip().lower(),))['user_id']
            result=import_project(relay,actor,args.repo,args.name)
            print('Imported',result['files'],'committed files. Project:',result['project_id'],'Checkpoint:',result['checkpoint'])
        finally:relay.close()

if __name__=='__main__':main()
