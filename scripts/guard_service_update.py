#!/usr/bin/env python3
"""Explicit deployment helper; never invoked by tests or the worker against real systemd."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from leads.services.supervision import guarded_update


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--state-file', required=True)
    parser.add_argument('--change-script', required=True, help='Reviewed executable script; no shell interpolation.')
    parser.add_argument('--rollback-script', required=True, help='Reviewed executable restoring previous checkout/unit config.')
    args = parser.parse_args()
    for script in (args.change_script, args.rollback_script):
        if not Path(script).is_absolute() or not os.access(script, os.X_OK):
            parser.error('Both scripts must be existing absolute executable paths.')

    def record(state):
        fd = os.open(args.state_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())

    guarded_update(args.unit, lambda: subprocess.run([args.change_script], check=True),
                   lambda: subprocess.run([args.rollback_script], check=True), record)


if __name__ == '__main__':
    main()
