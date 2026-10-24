"""Create private local Codex homes or inspect auth via App Server; never copy tokens."""
import argparse
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
    args=parser.parse_args()
    if args.env_file:load_env_file(args.env_file)
    try:
        if args.action=='init':
            initialize_profiles()
            print('Created three private CODEX_HOME/workspace directories. No credentials copied. Authenticate each interactively:')
            for profile in PROFILE_IDS:
                home,_=profile_paths(profile)
                print('CODEX_HOME='+shlex.quote(str(home))+' codex -c \'cli_auth_credentials_store="file"\' login --device-auth')
        else:
            checks,details=native_readiness()
            for label,ok in checks:print(('PASS: ' if ok else 'MISSING: ')+label)
            for detail in details:print(detail)
            if not all(ok for _,ok in checks):raise SystemExit(2)
    finally:close_native_sessions()

if __name__=='__main__':main()
