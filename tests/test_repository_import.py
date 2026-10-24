"""Trusted operator import uses committed files, without source Git/auth state."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from relay.fs_lock import which_git
from relay.store import Relay
from relay.accounts import InviteIdentityProvider
from scripts.import_project import import_project

class RepositoryImportTests(unittest.TestCase):
    def test_import_preserves_code_and_excludes_auth_and_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'source';repo.mkdir()
            git_bin=which_git() or 'git'
            def git(*args):subprocess.run([git_bin,'-c','safe.directory=*','-C',str(repo),*args],capture_output=True,check=True)
            git('init','-q');git('config','user.name','Test');git('config','user.email','test@relay.invalid')
            (repo/'example.py').write_text('value = 42\n');(repo/'auth.json').write_text('TEST-ONLY-MUST-NOT-IMPORT');(repo/'.env').write_text('TEST_ONLY=private')
            git('add','.');git('commit','-qm','Fixture')
            relay=Relay(root/'relay',demo=False)
            try:
                user=InviteIdentityProvider(relay).bootstrap('operator@relay.invalid','Operator','Fixture password 2026!')['id']
                result=import_project(relay,user,repo,'Imported authorized code')
                files=relay.detail(user,result['project_id'])['workspace']['files']
                self.assertIn('example.py',files);self.assertNotIn('auth.json',files);self.assertNotIn('.env',files)
                self.assertEqual(relay.ws.git(relay.ws.path(result['project_id']),'log','-1','--format=%s'),'Import authorized repository snapshot')
                link=repo/'unsafe.py'
                try:link.symlink_to('outside-target')
                except (OSError,NotImplementedError):
                    blob=subprocess.check_output([git_bin,'-c','safe.directory=*','-C',str(repo),'hash-object','-w','--stdin'],input=b'outside-target')
                    git('update-index','--add','--cacheinfo',f'120000,{blob.decode().strip()},unsafe.py')
                else:git('add','unsafe.py')
                git('commit','-qm','Unsafe link')
                with self.assertRaisesRegex(ValueError,'Symlinks'):import_project(relay,user,repo,'Rejected snapshot')
            finally:relay.close()

if __name__=='__main__':unittest.main()
