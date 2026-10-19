# Operations and recovery

## Check health

`python3 manage.py status` (or `doctor`) reports LaunchAgent loading, HTTP readiness, polling heartbeat, current source errors, live timing statistics and the last native-bridge heartbeat. Loaded is not necessarily healthy; inspect both. Source access failures differ from a stopped collector.

The loopback `/health` returns 503 when the polling thread is not alive/fresh. The server exits if that loop dies or its heartbeat stalls for 90 seconds. launchd then restarts it. The native helper is supervised separately; a graceful menu quit stays stopped until `start` or next login.

The collector holds `data/radar.lock`; the helper holds `data/notifier.lock`. File existence does not mean a process is running: OS locks release on process exit. Do not delete a lock to run concurrent writers.

## Logs and data

Under `~/Library/Application Support/AI News Radar/data/`:

- `service.log*`: rotating per-source polling results.
- `collector.err.log` / `collector.out.log`: launch/startup failures.
- `notifier.err.log` / `notifier.out.log`: native-helper process diagnostics.
- `radar.sqlite3`, its WAL/SHM companions: live state. Use `manage.py backup` rather than copying just the DB file while running.

The launch stdout/stderr files are not automatically rotated by launchd. They should normally be quiet. Investigate a growing startup-error log rather than letting a crash loop run indefinitely. `manage.py disable` stops supervision without deleting history.

Per-source failures preserve the last good state and retry. HTTP 429 and Retry-After delay that host; certificate validation remains on. No paid fallback, authentication bypass or proxy is used. Large or truncated responses are rejected, not treated as a new baseline. Catalog paths may need maintenance when a provider changes its page or access rules.

## Upgrade failure

The installer builds/stages before stopping the service. It snapshots program files and plist bytes, replaces only its known program paths, waits for healthy local readiness, and attempts to restore/restart the prior program on failure. DB/history stays in place. Program snapshots are retained under the runtime's `backups/program-*`; they are not independent DB backups. Keep a private migration backup before larger upgrades.

If the port is held by another process, installation refuses to overwrite its listener. If a LaunchAgent label/plist belongs to another program path, management refuses to stop or replace it. Do not solve either conflict by killing arbitrary processes.

## View from another Mac

The dashboard is deliberately localhost-only. One option is an SSH tunnel using an SSH service/account you have already configured. Disable the old local collector first to free local port 8741, then:

```sh
ssh -N -L 8741:127.0.0.1:8741 YOUR_USER@YOUR_MAC_MINI
```

Open <http://127.0.0.1:8741/> on the client Mac while the tunnel is open. Keep the local forwarding port at 8741 for this config because HTTP Host is validated. This repository does not enable SSH, open firewall ports or set up remote accounts. Local native alerts appear on the Mac mini; a tunnel does not forward its notifications.

## Backup safety

Migration ZIPs contain a manifest, SQLite snapshot and config only. Restore accepts only those exact names, checks SHA-256 and SQLite integrity, and refuses an existing target DB. Do not bypass that refusal by deleting unique data. Restore old pending notifications as silent history, reset local bridge/PID metadata, and leave X/browser login state to the destination.

Source/API limits and Mac sleep/offline gaps can exceed the one-hour target. Old measurements are retained, so a mixed historical median is not a measurement of only the newest version's steady-state performance.
