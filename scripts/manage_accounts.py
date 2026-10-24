"""Offline, locked account bootstrap/revocation. No provider credentials accepted."""
import argparse
import getpass
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.store import Relay  # noqa: E402
from relay.accounts import InviteIdentityProvider  # noqa: E402
from relay.config import load_env_file  # noqa: E402
from scripts.demo import exclusive_demo_directory  # noqa: E402


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('bootstrap','revoke-sessions'))
    parser.add_argument('--data',default='shared-data');parser.add_argument('--email',required=True)
    parser.add_argument('--name',default='Organization administrator');parser.add_argument('--env-file',type=Path)
    parser.add_argument('--local-owner',action='store_true',help='Pin the new sole Relay account to this OS user for local-native dashboard mode')
    args=parser.parse_args()
    if args.env_file:load_env_file(args.env_file)
    password=None
    if args.action=='bootstrap':
        password=getpass.getpass('New Relay administrator password (12+ characters): ')
        if password!=getpass.getpass('Repeat Relay password: '):parser.error('Passwords did not match')
    with exclusive_demo_directory(args.data):
        relay=Relay(args.data,demo=False)
        try:
            identity=InviteIdentityProvider(relay)
            if args.action=='bootstrap':
                identity.bootstrap(args.email,args.name,password,local_owner=args.local_owner)
                print('Local owner pinned. Start with --auth local-owner --backend native.' if args.local_owner else 'Administrator created. Start Relay with invite authentication.')
            else:
                account=relay.one('SELECT user_id FROM accounts WHERE email=?',(args.email.strip().lower(),))
                relay.db.execute('DELETE FROM sessions WHERE user_id=?',(account['user_id'],))
                relay.audit(None,'offline_sessions_revoked',metadata={'member':account['user_id']})
                print('All Relay sessions for this account revoked.')
        finally:relay.close()

if __name__=='__main__':main()
