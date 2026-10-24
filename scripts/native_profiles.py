"""Create private local Codex homes or inspect auth via App Server; never copy tokens."""
import argparse
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.native import initialize_profiles,PROFILE_IDS,profile_paths,native_readiness,close_native_sessions  # noqa: E402
from relay.config import load_env_file  # noqa: E402

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','check'))
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--profile',choices=PROFILE_IDS,help='Check one existing profile without changing its Codex login')
    args=parser.parse_args()
    if args.env_file:load_env_file(args.env_file)
    try:
        if args.action=='init':
            if args.profile:parser.error('--profile is supported for check only')
            initialize_profiles()
            print('Created three private CODEX_HOME/workspace directories. No credentials copied. Authenticate each interactively:')
            for profile in PROFILE_IDS:
                home,_=profile_paths(profile)
                quoted=shlex.quote(str(home))
                if os.name=='nt':
                    print("$env:CODEX_HOME="+quoted+"; codex -c 'cli_auth_credentials_store=\"file\"' login --device-auth")
                else:
                    print('CODEX_HOME='+quoted+' codex -c \'cli_auth_credentials_store="file"\' login --device-auth')
        else:
            checks,details=native_readiness((args.profile,) if args.profile else PROFILE_IDS)
            for label,ok in checks:print(('PASS: ' if ok else 'MISSING: ')+label)
            for detail in details:print(detail)
            if not all(ok for _,ok in checks):raise SystemExit(2)
    finally:close_native_sessions()

if __name__=='__main__':main()
