"""Explicit user-systemd state inspection and restoration; never called by the worker."""
import re
import subprocess
from pathlib import Path


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def snapshot(unit):
    if not unit or unit.startswith('-') or not unit.endswith(('.service', '.timer')):
        raise ValueError('Expected an explicit service or timer unit name.')
    values = dict(line.split('=', 1) for line in systemctl('show', unit,
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'WorkingDirectory', '-p', 'ExecStart').splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded':
        raise ValueError('Unit is not loaded; no change made.')
    # Extract only checkout paths, never print ExecStart (it may contain secrets).
    entry_checkouts = set()
    for path in re.findall(r"/[^\s;{}\"']+", values.get('ExecStart', '')):
        for suffix in ('/.venv/bin/python', '/manage.py', '/run-local.py'):
            if path.endswith(suffix):
                entry_checkouts.add(path[:-len(suffix)])
    return {'entry_checkouts': sorted(entry_checkouts), 'unit': unit, 'active_state': values.get('ActiveState', 'unknown'),
            'enabled_state': values.get('UnitFileState', 'unknown'),
            'working_directory': values.get('WorkingDirectory', '')}


def validate_restorable(state):
    if state['enabled_state'] not in ('enabled', 'disabled') or state['active_state'] not in ('active', 'inactive', 'failed'):
        raise ValueError('Unsupported/masked/transitional unit state; inspect manually before deployment.')


def restore(state):
    """Restore enablement AND liveness, then require successful readback."""
    validate_restorable(state)
    systemctl('enable' if state['enabled_state'] == 'enabled' else 'disable', state['unit'])
    systemctl('start' if state['active_state'] == 'active' else 'stop', state['unit'])
    actual = snapshot(state['unit'])
    expected_active = 'active' if state['active_state'] == 'active' else 'inactive'
    if actual['enabled_state'] != state['enabled_state'] or actual['active_state'] != expected_active:
        raise RuntimeError('Service state restoration failed readback; operator recovery required.')
    return actual


def revision(directory):
    if not directory or not Path(directory).is_dir():
        return None
    try:
        return subprocess.run(['git', '-C', directory, 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def status(unit, expected_revision):
    state = snapshot(unit)
    current = revision(state['working_directory'])
    entries = [revision(path) for path in state.get('entry_checkouts', [])]
    stale = current != expected_revision or any(sha != expected_revision for sha in entries)
    return {**state, 'entry_revisions': entries, 'active': state['active_state'] == 'active',
            'enabled': state['enabled_state'] == 'enabled',
            'stopped': state['active_state'] in ('inactive', 'failed'),
            'revision': current, 'checkout_status': 'unknown' if current is None or not expected_revision
                else 'stale_checkout' if stale else 'current'}


def guarded_update(unit, change, rollback, record):
    """Callbacks own the checkout/config switch. Always restore prior systemd state.

    Not a database rollback: only use backward-compatible code/schema changes, backed up
    separately. SIGKILL/power loss still require the recorded-state recovery procedure.
    """
    state = snapshot(unit)
    validate_restorable(state)
    record(state)  # durable preflight must succeed BEFORE stopping anything
    try:
        systemctl('stop', unit)
        change()
        restore(state)
    except BaseException:
        try:
            rollback()
        finally:
            restore(state)
        raise
    return state
