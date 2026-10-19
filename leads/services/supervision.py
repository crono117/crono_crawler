"""Explicit user-systemd inspection and guarded updates. Never called by the worker.

Only direct .venv Python service entry points and inspectable unit/config files are
supported by the update guard. Unknown wrappers/configuration fail before any stop.
This coordinates ONE unit; callers must separately quiesce all other DB writers.
"""
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path


class RecoveryRequired(RuntimeError):
    """Quiescence or release identity could not be established; never restart."""


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def _paths(value):
    # systemd escapes not supported here: ambiguity must never pass a guard.
    if '\\' in value:
        raise ValueError('Escaped systemd paths need manual review.')
    values = shlex.split(value)
    if any(not Path(path).is_absolute() for path in values):
        raise ValueError('Expected absolute systemd file paths.')
    return values


def snapshot(unit):
    if not unit or unit.startswith('-') or not unit.endswith(('.service', '.timer')):
        raise ValueError('Expected an explicit service or timer unit name.')
    properties = ('LoadState', 'ActiveState', 'UnitFileState', 'WorkingDirectory', 'ExecStart',
                  'MainPID', 'ControlPID', 'ControlGroup', 'KillMode', 'FragmentPath',
                  'DropInPaths', 'EnvironmentFiles', 'NeedDaemonReload', 'Environment', 'User', 'Group', 'PassEnvironment', 'UnsetEnvironment')
    values = dict(line.split('=', 1) for line in systemctl('show', unit,
        *[arg for prop in properties for arg in ('-p', prop)]).splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded':
        raise ValueError('Unit is not loaded; no change made.')
    inspected = True
    entry_paths, checkouts, unit_files, environment_files = [], [], [], []
    # Do not persist ExecStart or Environment values: argv may contain secrets.
    try:
        raw = values.get('ExecStart', '')
        starts = re.findall(r'\{\s*path=([^;]+);\s*argv\[\]=([^;]+);', raw)
        if len(starts) != 1 or '\\' in raw:
            raise ValueError('Unrecognized ExecStart.')
        executable, argv = starts[0][0].strip(), shlex.split(starts[0][1].strip())
        args = argv[1:]
        if args and args[0] == '-u':
            args = args[1:]
        if (not argv or argv[0] != executable or not args or not Path(args[0]).is_absolute()
                or Path(args[0]).name not in ('manage.py', 'run-local.py')):
            raise ValueError('Wrapper or uninspected entry point.')
        root = Path(args[0]).parent
        if executable != str(root / '.venv/bin/python'):
            raise ValueError('Entry point and interpreter checkouts differ.')
        entry_paths, checkouts = [executable, args[0]], [str(root)]
        unit_files = _paths(values.get('FragmentPath', '')) + _paths(values.get('DropInPaths', ''))
        env = values.get('EnvironmentFiles', '')
        # Typical systemctl show format: /absolute/path (ignore_errors=no)
        environment_files = _paths(re.sub(r'\s+\(ignore_errors=(?:yes|no)\)', '', env))
        if not unit_files:
            raise ValueError('No unit files.')
    except ValueError:
        inspected = False
    # Hash stable, effective configuration only; ExecStart includes runtime PID and
    # timestamps, so its full string is deliberately excluded. No secret values persist.
    effective = {key: values.get(key, '') for key in ('WorkingDirectory', 'KillMode', 'Environment',
                 'EnvironmentFiles', 'User', 'Group', 'PassEnvironment', 'UnsetEnvironment')}
    effective['argv'] = argv if inspected else []
    config_hash = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
    return {'effective_config_hash': config_hash, 'unit': unit, 'active_state': values.get('ActiveState', 'unknown'),
            'enabled_state': values.get('UnitFileState', 'unknown'),
            'working_directory': values.get('WorkingDirectory', ''),
            'main_pid': int(values.get('MainPID', '-1')), 'control_pid': int(values.get('ControlPID', '-1')),
            'control_group': values.get('ControlGroup', ''), 'kill_mode': values.get('KillMode', ''),
            'entry_checkouts': checkouts, 'entry_paths': entry_paths,
            'unit_files': unit_files, 'environment_files': environment_files,
            'entrypoints_inspected': inspected, 'definition_reloaded': values.get('NeedDaemonReload') == 'no'}


def validate_restorable(state):
    if (not state['unit'].endswith('.service') or state['enabled_state'] not in ('enabled', 'disabled')
            or state['active_state'] not in ('active', 'inactive')):
        raise ValueError('Guard requires a service with stable enabled/disabled and active/inactive state.')


def checkout_info(directory):
    if not directory or not Path(directory).is_dir():
        return {'revision': None, 'dirty': None}
    try:
        sha = subprocess.run(['git', '-C', directory, 'rev-parse', 'HEAD'], check=True,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', directory, 'status', '--porcelain', '--untracked-files=normal'],
                                   check=True, capture_output=True, text=True, timeout=5).stdout)
        return {'revision': sha, 'dirty': dirty}
    except (OSError, subprocess.SubprocessError):
        return {'revision': None, 'dirty': None}


def revision(directory):
    return checkout_info(directory)['revision']


def status(unit, expected_revision):
    state = snapshot(unit)
    current = checkout_info(state['working_directory'])
    entries = [checkout_info(path) for path in state['entry_checkouts']]
    inspected = state['entrypoints_inspected'] and current['revision'] and entries and all(e['revision'] for e in entries)
    if not inspected:
        label = 'uninspected_checkout'
    elif current['dirty'] or any(e['dirty'] for e in entries):
        label = 'dirty_checkout'
    elif not expected_revision:
        label = 'unknown'
    elif any(info['revision'] != expected_revision for info in [current, *entries]):
        label = 'stale_checkout'
    else:
        label = 'current'
    return {**state, 'revision': current['revision'], 'dirty': current['dirty'],
            'entry_revisions': [info['revision'] for info in entries],
            'checkout_status': label, 'active': state['active_state'] == 'active',
            'enabled': state['enabled_state'] == 'enabled', 'stopped': state['active_state'] in ('inactive', 'failed')}


def capture_identity(state, extra_files=()):
    """Only hashes/paths/revisions persist; never store private configuration bytes."""
    if not state['entrypoints_inspected'] or not state['definition_reloaded']:
        raise RecoveryRequired('Unit/config identity is uninspected or requires daemon-reload.')
    directories = sorted(set([state['working_directory'], *state['entry_checkouts']]))
    checkouts = {path: checkout_info(path) for path in directories}
    if not directories or any(not info['revision'] or info['dirty'] is not False for info in checkouts.values()):
        raise RecoveryRequired('Checkout identity requires clean, inspectable Git checkouts.')
    files = set(state['unit_files'] + state['environment_files'] + state['entry_paths'] + list(extra_files))
    for directory in directories:
        env = Path(directory) / '.env'
        if env.exists():
            files.add(str(env))
    hashes = {}
    for name in sorted(files):
        path = Path(name)
        if not path.is_absolute() or not path.is_file():
            raise RecoveryRequired('Unit/config identity file is missing or uninspectable.')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'checkouts': checkouts, 'file_hashes': hashes, 'working_directory': state['working_directory'],
            'entry_paths': state['entry_paths'], 'unit_files': state['unit_files'],
            'environment_files': state['environment_files'], 'effective_config_hash': state['effective_config_hash'],
            'extra_files': list(extra_files)}


def verify_identity(previous):
    actual = capture_identity(snapshot(previous['unit']), previous['identity'].get('extra_files', ()))
    if actual != previous['identity']:
        raise RecoveryRequired('Rollback identity differs from the recorded checkout/unit/config identity.')


def cgroup_empty(state):
    group = state['control_group']
    if not group:
        return True  # systemd reports no remaining cgroup; PID gates checked separately
    if not group.startswith('/') or '..' in Path(group).parts or group == '/':
        return False
    root = Path('/sys/fs/cgroup')
    path = root / group.lstrip('/')
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return False
        values = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
        return values.get('populated') == '0'
    except FileNotFoundError:
        return not path.exists()  # removed group is empty; existing cgroup-v1 group is unproven
    except (OSError, ValueError):
        return False


def require_quiescent(unit):
    state = snapshot(unit)
    if (state['active_state'] != 'inactive' or state['main_pid'] != 0 or state['control_pid'] != 0
            or state['kill_mode'] != 'control-group' or not cgroup_empty(state)):
        raise RecoveryRequired('Service quiescence unproven; rollback/change forbidden; operator recovery required.')
    return state


def quiesce(unit):
    systemctl('stop', unit)
    return require_quiescent(unit)


def restore(state, *, restart=True):
    should_start = restart and state['active_state'] == 'active'
    require_quiescent(state['unit'])
    systemctl('enable' if state['enabled_state'] == 'enabled' else 'disable', state['unit'])
    if should_start:
        systemctl('start', state['unit'])
    actual = snapshot(state['unit'])
    if actual['enabled_state'] != state['enabled_state'] or actual['active_state'] != ('active' if should_start else 'inactive'):
        raise RecoveryRequired('Service state restoration failed readback; operator recovery required.')
    return actual


def _leave_stopped(state):
    # Best effort after a failed stop: no callbacks and absolutely no start. An
    # inability to prove inactivity still propagates as a recovery-required error.
    try:
        quiesce(state['unit'])
    finally:
        systemctl('enable' if state['enabled_state'] == 'enabled' else 'disable', state['unit'])


def guarded_update(unit, change, rollback, record, *, identity_files=()):
    """One-unit guard. Verified stop before any change and EVERY rollback."""
    state = snapshot(unit)
    validate_restorable(state)
    state['identity'] = capture_identity(state, identity_files)
    record(state)
    try:
        quiesce(unit)
    except BaseException as exc:
        try:
            _leave_stopped(state)
        except BaseException:
            pass
        raise RecoveryRequired('Initial quiescence failed; no change or rollback ran.') from exc
    try:
        change()
        # A change script must not start the service itself.
        fresh = require_quiescent(unit)
        capture_identity(fresh, identity_files)  # new checkout/config must also be inspected and clean
        restore(state)
    except BaseException:
        try:
            quiesce(unit)  # including when a prior restore already issued start
        except BaseException as exc:
            try:
                _leave_stopped(state)
            except BaseException:
                pass
            raise RecoveryRequired('Rollback forbidden: quiescence unproven; operator recovery required.') from exc
        try:
            rollback()
            quiesce(unit)  # callback must leave writers stopped, even on partial rollback
            verify_identity(state)
        except BaseException:
            _leave_stopped(state)
            raise
        restore(state)
        raise
    return state
