"""Operator-only native tool/dependency preparation; never reads Codex credentials."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.native_environment import prepare_dependencies, provision_node, toolchain  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    node = sub.add_parser('approve-node', help='Copy this operator-approved executable and integrity-pinned npm into the private store')
    node.add_argument('--node', type=Path, required=True)
    prepare = sub.add_parser('prepare', help='Download committed registry dependencies without running install scripts')
    prepare.add_argument('--workspace', type=Path, required=True)
    sub.add_parser('check')
    args = parser.parse_args()
    try:
        if args.action == 'approve-node':
            print(provision_node(args.node))
        elif args.action == 'prepare':
            print('Prepared offline bundle: ' + prepare_dependencies(args.workspace))
        else:
            print('Approved Node/npm configured' if toolchain() else 'No approved Node/npm; Python-only tasks remain available')
    except (ValueError, OSError) as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
