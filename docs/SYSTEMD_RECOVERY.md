# User-systemd recovery and future deployment procedure

These commands are an operator procedure, not actions performed by this change.
Inspect the existing unit and drop-ins before installing anything. Keep the established
service name and database. Do not create an additional collector unit. The unit template
owns run-local.py, whose two children are the separable web and worker processes.

## Read-only preflight and reboot verification

Run as the existing service user, from the approved checkout with its established configuration:

```bash
systemctl --user is-enabled crono-lead-engine.service
systemctl --user is-active crono-lead-engine.service
systemctl --user show crono-lead-engine.service -p FragmentPath -p DropInPaths -p WorkingDirectory -p ActiveState -p UnitFileState
loginctl show-user "$USER" -p Linger
systemctl --user list-timers --all
.venv/bin/python manage.py crawler_status --campaign 2 \
  --unit crono-lead-engine.service --unit ACTUAL_COMMONCRAWL_UNIT.service \
  --expected-revision INTENDED_FULL_SHA --http http://127.0.0.1:8017/
```

Replace placeholders with inspected unit names and the intended approved commit. Never
paste an entire Environment/ExecStart dump into a report: it may contain secrets. The status
command extracts only checkout paths and git revisions from ExecStart and WorkingDirectory.
It reports active/enabled/stopped separately, stale checkout, stale/empty/missing/fresh lease,
HTTP health, job queues and terminal reasons. It makes no target/provider requests. The
optional numeric-loopback HTTP probe does not follow redirects and reads no response body.
HTTP 200/302 only proves the console answers; a fresh lease and queue progress are separate checks.
A stale lease with an empty token is shown as empty with its heartbeat age, not as an active worker.

Compare the lead unit and ALL Common Crawl entry-point revisions against the intended release.
A unit using b2161bc while the collector uses 10f37e8 is stale. Correct the inspected unit's
checkout references only during a separately authorized release, then daemon-reload and read back.
An isolated Jev soak service/timer intentionally owns a DIFFERENT synthetic database and may own
a different checkout. Inspect it separately; never include it in production revision alignment.

After an authorized reboot, repeat every readback above. Check one owning launcher/worker
process tree privately and verify the lease becomes fresh and existing run/job IDs advance.
Lease takeover can wait up to 600 seconds after a hard stop. Do not delete lease tokens or
create a second worker to hurry it. Queued jobs retain deferrals; blocked/paused sources stay gated.

## Explicit, idempotent installation / upgrade

First record the original enabled/active state and private unit/drop-in backups. Review the
tracked template `deploy/systemd/crono-lead-engine.service.in`. Substitute @CHECKOUT@ with
the absolute approved checkout and @CONFIG@ with the existing private configuration file
path; do not copy credentials into the repository or command output. Paths must be systemd-escaped
if they contain spaces or specifiers. Render to a temporary file and validate it before install.

```bash
systemd-analyze --user verify /absolute/path/to/rendered/crono-lead-engine.service
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 /absolute/path/to/rendered/crono-lead-engine.service "$HOME/.config/systemd/user/crono-lead-engine.service"
systemctl --user daemon-reload
```

`install`, daemon-reload and the following enable commands are repeatable. Preserve existing
drop-ins; compare their effective WorkingDirectory and ExecStart because overrides can keep an
old checkout even after installing a new base unit. Do not clear overrides automatically.
Enabling continuous boot operation is a deliberate operator action, distinct from preserving
a prior disabled state during an ordinary upgrade:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user enable --now crono-lead-engine.service
systemctl --user is-enabled crono-lead-engine.service
systemctl --user is-active crono-lead-engine.service
loginctl show-user "$USER" -p Linger
```

The user manager starts default.target; WantedBy=default.target supplies its startup link.
Linger keeps that user manager available at boot without interactive login. Restart=on-failure
recovers launcher/child failures, subject to systemd's rate limits. Check `systemctl --user status`
and `journalctl --user -u crono-lead-engine.service -b` privately after repeated failures.
Neither restart policy nor a reboot overrides an explicit disabled service. Enabling a timer
starts only its own schedule, not this collector. The separate Jev timer is never enabled by this procedure.

## State-preserving deployment guard

The optional helper is for a FUTURE authorized, reviewed release:

```bash
python3 scripts/guard_service_update.py --unit crono-lead-engine.service \
  --state-file /private/backup/release-UNIQUE-state.json \
  --change-script /absolute/path/to/reviewed-change-script \
  --rollback-script /absolute/path/to/reviewed-rollback-script
```

It writes an exclusive mode-0600 state file before stopping the unit, rejects masked/static/
transitional states, runs the executable change script without shell interpolation, and restores
both prior enablement and activity with readback. A failing change runs the rollback script
first, then restores service state even if rollback fails; errors propagate rather than claiming
success. Scripts own the actual checkout/unit switch and private backups; this helper alone
cannot undo an application configuration change. Rollback must restore the PREVIOUS code/unit
before starting its service. It must not restore an old database and discard later evidence.
The change script must do local preflight, supported migrations, check and collectstatic before
returning successfully. Normal disabled deployments stay disabled; first boot enablement is separate.

A SIGKILL, power loss or unavailable user manager can interrupt any script. The durable state
file supports explicit recovery: restore the reviewed prior checkout/unit configuration, reload
systemd, then enable/disable and start/stop according to that file. Verify actual states, revision,
HTTP, single worker and lease afterward. Never claim an in-process finally block survives power loss.

Migration 0011 backfills old failed/paused messages as legacy_unknown and enforces nonempty
terminal reasons/messages. Existing errors cannot be reconstructed. Older code cannot safely
write terminal rows under the new constraint. A rollback to the baseline must stop this worker,
reverse discovery 0011 with the new code (on a backup-tested database), then switch code; reversal
drops ONLY the new diagnostics columns/constraint. Existing lead/review data is retained. The
prior architecture/automation rollback cautions also apply. No migration was run on production.
