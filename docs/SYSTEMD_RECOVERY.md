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
It reports active/enabled/stopped separately, stale/dirty/uninspected checkout, stale/empty/missing/fresh lease,
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
path; do not copy credentials into the repository or command output. The automated guard
supports unambiguous absolute paths and direct .venv/bin/python entry points only. Escaped
paths, shell wrappers or alternate entry points require a separate reviewed manual procedure;
the guard refuses them before stopping anything. Render to a temporary file and validate it before install.

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

The user manager's network-online.target ordering is only a hint and does not establish host
network readiness. The existing transport retries transient network failures with persisted
backoff; failed processes are restarted by the unit. After boot, verify actual HTTP/lease/queue
progress rather than treating target ordering as a readiness probe.

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
  --rollback-script /absolute/path/to/reviewed-rollback-script \
  --identity-file /absolute/path/to/additional-reviewed-config
```

The guard records a clean prior Git revision for every recognized checkout, SHA-256 hashes
of unit fragments/drop-ins, declared EnvironmentFiles, the interpreter and script, checkout
.env files when present, and any additional --identity-file dependencies. It also hashes
stable effective unit configuration (including explicit environment and argv) without persisting
its raw secret values. This fingerprint covers the declared files/configuration; it is not a
snapshot of the entire host, installed dependencies, manager environment, or database. The
reviewed release must explicitly inventory any other configuration dependencies.

The exclusive mode-0600 state file AND its parent directory are fsynced before any stop.
Preflight refuses dirty/uninspectable checkouts, unknown entry wrappers, pending daemon-reload,
missing identity files, masked/static/transitional units, and service states other than active
or inactive. Only direct .venv/bin/python followed by an absolute manage.py or run-local.py
(and optional -u) is recognized. A status report never labels unknown wrappers current.

Before change and EVERY rollback, it issues stop and verifies ActiveState=inactive, MainPID=0,
ControlPID=0, KillMode=control-group, and an empty or removed cgroup (cgroup v2 populated=0 when
a group remains). A stop return code alone is not proof. If these checks fail, no change/rollback
callback runs; the helper attempts a stop, preserves prior enablement as far as possible,
never starts the unit, and reports operator recovery required. It does not bypass an unavailable
user manager or uncertain cgroup state.

A change script must leave the service stopped and the new checkout clean/config inspectable.
If post-start readback fails, the guard stops and proves quiescence AGAIN before rollback.
A zero-exit rollback script is not enough: after rollback, the guard stops/checks the unit again
and verifies the recorded code/config fingerprints before restarting a previously active unit.
No-op/partial rollback, dirty or wrong revision, altered configuration, or failed rollback leaves
the unit stopped (as far as can be proven), preserves prior enablement, and raises an error.
The unit must not be rebooted/started until operator recovery restores the known configuration.
Successful rollback restores prior enablement/activity but still propagates the deployment error.

Scripts own the actual checkout/unit/schema switch and private backups. They must not start
services themselves, modify shared code under live writers, silently restore an old database,
or discard later observations. The change script must perform reviewed preflight/migration,
check and collectstatic, daemon-reload when needed, and verify the intended new revision before
returning. Use an immutable clean checkout for the release and for this guard; one operator
must own the update, with other timers/automation disabled under the coordinated outage plan.
The helper validates one unit, not concurrent administrator changes or the entire database's
writer population. Normal disabled deployments stay disabled; boot enablement is separate.

A SIGKILL, power loss or unavailable user manager can interrupt any script. The durable state
file supports explicit recovery: restore the reviewed prior checkout/unit configuration, reload
systemd, then enable/disable and start/stop according to that file. Verify actual states, revision,
HTTP, single worker and lease afterward. Never claim an in-process finally block survives power loss.

Before migrating, quiesce ALL database writers, including the independent Common Crawl
service/timer, and record each unit's original state. This helper guards only its named unit;
it does not stop sibling timers. The reviewed deployment scripts must handle those units
and restore their prior state only with compatible checkout references. Preserve the separate
synthetic Jev soak configuration. A stale Common Crawl checkout must not write the upgraded DB.

Migration 0011 backfills old failed/paused messages as legacy_unknown and enforces nonempty
terminal reasons/messages. Existing errors cannot be reconstructed. Older code cannot safely
write this schema: new required JSON columns lack database defaults for old ORM inserts,
and the new constraint also rejects old blank terminal updates. A throwaway historical-model
insert reproduced this incompatibility. Stop all old-code writers before migration, and align
Common Crawl with the intended revision before resuming it. A rollback to the baseline must stop this worker,
reverse discovery 0011 with the new code (on a backup-tested database), then switch code; reversal
drops ONLY the new diagnostics columns/constraint. Existing lead/review data is retained. The
prior architecture/automation rollback cautions also apply. No migration was run on production.

## Required release evidence before migration 0011

Maintain an operator-reviewed writer inventory: each service/timer/cron/manual process,
actual database path/inode, checkout SHA and dirty status, entry point, enabled/active state,
and stop/resume owner. Stop scheduling triggers first, stop every writer, verify unit PID/cgroup
emptiness and absence of manual writers, then use the SQLite Backup API for a fresh private copy.
Do not assume list-timers or the one-unit guard finds every writer. The isolated Jev soak must
remain on its own synthetic database/configuration and outside production revision alignment.

On that fresh copy, check SQLite integrity, retained row counts and decisions; record migration
0010→0011 timing, peak copy/table-rebuild disk usage and available disk; rehearse reversal and
reapplication, then run the offline checks and read-only status against the copy. SQLite 0011
rebuilds tables multiple times, so synthetic timings are not a production downtime estimate.
Old-writer incompatibility remains a release gate, not a reason to weaken schema constraints.
No actual production-backup rehearsal, writer inventory, installed-unit verification, or host
network/reboot validation has been performed here. A new independent review of the amended
exact commit and separate deployment authorization are required.
