#!/usr/bin/env python3
"""Explicit deployment helper; never invoked by tests or the worker against real systemd."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from leads.services.supervision import RecoveryRequired, guarded_update


def write_state(path, state):
    """Persist file contents AND its directory entry before any service stop."""
    path = Path(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_state(path):
    """Unreadable durable rollback evidence must never authorize a restart."""
    try:
        with Path(path).open() as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RecoveryRequired('Rollback state file is unreadable or invalid.') from exc
    if not isinstance(state, dict):
        raise RecoveryRequired('Rollback state file is missing or invalid.')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--state-file', required=True)
    parser.add_argument('--change-script', required=True, help='Reviewed executable script; no shell interpolation.')
    parser.add_argument('--rollback-script', required=True, help='Reviewed executable restoring previous checkout/unit config.')
    parser.add_argument('--identity-file', action='append', default=[],
                        help='Additional reviewed config/dependency file to hash; repeat as needed.')
    args = parser.parse_args()
    for script in (args.change_script, args.rollback_script):
        if not Path(script).is_absolute() or not os.access(script, os.X_OK):
            parser.error('Both scripts must be existing absolute executable paths.')

    guarded_update(args.unit, lambda: subprocess.run([args.change_script], check=True),
                   lambda: subprocess.run([args.rollback_script], check=True),
                   lambda state: write_state(args.state_file, state), identity_files=args.identity_file,
                   read_record=lambda: read_state(args.state_file))


if __name__ == '__main__':
    main()
