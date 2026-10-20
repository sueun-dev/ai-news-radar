# AI News Radar

A local, free-to-run AI news collector for a Mac, with a Korean dashboard and a native notification helper. No X API, paid crawler, LLM API, proxy subscription or hosted server is required. It stores headline/link/time/hash metadata, not full articles or model weights.

The catalog contains **152 public collection paths across 101 hosts**: official announcements, Hugging Face model lists, product blogs, international and Korean media, research, communities, multilingual news search and policy sources. GitHub release/repository feeds are deliberately not collected. Paths are not distinct publishers. Availability and feed windows change; this is not a complete index of all AI news.

## Mac mini quick start

Requirements: macOS 13+, Python 3.9+ with SQLite 3.25+, and Apple's Command Line Tools for the notification app. No pip dependencies. The app builds for the current Mac's architecture and is signed locally without a paid developer account.

```sh
# If Command Line Tools are not installed, finish Apple's installer first:
xcode-select --install

git clone https://github.com/sueun-dev/ai-news-radar.git
cd ai-news-radar
python3 -m unittest discover -s tests -v
python3 manage.py install --keep-awake
python3 manage.py status
```

Open <http://127.0.0.1:8741/>. Allow **AI News Radar** notifications on this Mac, then use **시험 알림**. macOS accepting a notification does not prove that a banner was displayed. Permissions and X/browser sessions must be configured separately on each device.

`--keep-awake` runs the collector through `caffeinate -i` while it is running. It prevents idle system sleep; it does not keep the display on, override deliberate sleep, survive power loss or replace logging in. The LaunchAgents run after user login and stop at logout. For a collector without desktop alerts, use `install --no-notifications --keep-awake`; this does not require a Swift build.

For an existing database, follow the [Mac mini migration guide](docs/MAC_MINI.ko.md) **before** installing. A new install treats its first successful feed contents as a silent baseline.

## What runs in the background

- A user LaunchAgent supervises the collector and restarts it after a crash. A poller watchdog exits if the polling loop dies or stops updating its heartbeat, allowing launchd to restart it.
- A separate LaunchAgent starts the native notification helper at login and restarts it after an unsuccessful exit. Quitting the helper from its menu is a graceful exit; `manage.py start` starts it again.
- Collector and helper locks prevent two processes from using the same installed data directory. Separate Macs are independent; disable the old Mac's collector after verifying the new one.
- Installation stages and builds code before stopping the old service, waits for local health, and restores the previous program/plists if the new collector fails readiness. Database files are retained across code upgrades. Previous program snapshots remain in `backups/` under the installed runtime.
- SQLite WAL stores source state, items, events, preferences and HTTP checks. HTTP checks retain 30 days; item/event history is retained. The service log rotates at 2 MB with three backups.

Installed code/data: `~/Library/Application Support/AI News Radar/`.
LaunchAgents: `~/Library/LaunchAgents/com.sueun.ai-news-radar*.plist`.
The dashboard binds to **127.0.0.1 only**, validates Host/Origin, and is not a remotely exposed web service.

## Collection policy and timing

| Sources | Default interval |
|---|---:|
| Official announcements, Korean sources, selected major releases | 5 minutes |
| General feeds, models, blogs, media, news queries, communities, other releases | 10 minutes |
| Sitemaps | 15 minutes |
| Papers, policy, GitHub repository lists | 30 minutes |

Approximately one-hour freshness is a **target**, not an SLA. Publisher/CDN/indexing delays, feed truncation, access restrictions, server backoff and an offline or sleeping Mac can cause longer gaps or missed items. Polling more often does not guarantee earlier public exposure.

Four workers process independent hosts; each host has one active request and a three-second gap. Priority gives a finite head start so old retries eventually overtake fresh work. ETag/Last-Modified revalidation, 304 responses, gzip, bounded response bodies, read budgets and exponential retries reduce unnecessary work. `Retry-After` and provider polling limits take precedence.

The **지연 측정** panel and `events.csv` distinguish source-declared publication time, feed-update time, detection, observation gaps and macOS acceptance. Search-engine, community, paper-batch, sitemap and repository-creation clocks are not relabeled as original publication time. Baselines, synthetic tests and manual X observations do not count as live publication-delay samples. The dashboard currently formats times in America/Chicago; DB/CSV timestamps are Unix seconds.

`checks.csv` also records queue delay, transferred body bytes (without HTTP/TLS overhead), decoded size and content encoding. Empty fields indicate older records or unavailable measurements. A 304 has no response body. With 10–30 minute polling, the UI's “observation gap over five minutes” count can include normal scheduled checks.

## Commands

```sh
python3 manage.py status       # collector/helper health, source errors, timing
python3 manage.py doctor       # same current diagnostic snapshot
python3 manage.py test         # synthetic macOS notification, excluded from news timing
python3 manage.py stop         # stop now; login startup remains
python3 manage.py start
python3 manage.py disable      # stop and remove this app's login jobs; keep data
```

Updates preserve the installed config and database:

```sh
git pull --ff-only
python3 -m unittest discover -s tests -v
python3 manage.py install --keep-awake
```

Installer flags are explicit per install: repeat `--keep-awake` and/or `--no-notifications` when updating. To replace an installed configuration, pass `--config /absolute/path/config.json`. The chosen port is also embedded into the locally built notification helper. Do not run `radar.py` in a clone in parallel with the installed collector if you want a single collection stream; locks apply to one data directory, not multiple clones or Macs.

## Catalog and customization

[SOURCES.csv](SOURCES.csv) lists URLs, categories, intervals and default alert preferences. [sources.opml](sources.opml) contains feed paths only. [SOURCE_GUIDE.md](SOURCE_GUIDE.md) describes formats and coverage limits. [coverage.json](coverage.json) includes unavailable candidate paths from the original catalog check; it is a historical discovery record, not a live health result.

The **감시 소스** checkboxes change notification preferences without stopping collection and survive restarts. Research/community/search/sitemap paths default to collection without alerts. Prereleases and URL duplicates are recorded separately. Keyword filters can produce false positives and false negatives. There is no paid semantic deduplication or automatic translation/summarization.

The X tab links official accounts and supports a manual observation record. The collector does not log into, scrape or read notifications from X/Threads. Set official X notifications in the X app/browser yourself.

## Private backup and restore

```sh
# On the old Mac: safe while collection is running
python3 manage.py backup --output ../ai-news-radar-private.zip

# Transfer privately; then on a fresh destination, before install:
python3 manage.py restore --input /path/to/ai-news-radar-private.zip
python3 manage.py install --keep-awake
```

Backups contain private history/preferences and config, are created with mode 0600, and must not be committed or uploaded to a public repository. Restore checks checksums and SQLite integrity, refuses an existing target DB, retains history/preferences, clears old bridge status and silences old pending notifications. X/browser permission snapshots and credentials are not migrated. The same fresh backup cannot be restored over an installed database; preserve that database rather than deleting it to force a restore.

See [operations and recovery](docs/OPERATIONS.md) for logs, remote viewing and handoff.

## Code layout

`radar.py` is the collector entry point (the LaunchAgent runs it); the implementation is the `newsradar` package:

| Module | Responsibility |
|---|---|
| `timeutil` | ISO timestamps; parsing source times into an instant, a date or unknown |
| `items` | text cleaning, canonical URLs, AI-term matching, the collected-item record |
| `parsers` | one parser per source `kind`: `feed`, `sitemap`, `hf_models`, `anthropic`, `links`, `changelog` |
| `policy` | next-poll interval: cache freshness, `Retry-After`/`X-Poll-Interval` (capped at one day), backoff |
| `fetcher` | conditional, size- and time-bounded HTTP fetch of one source |
| `store` | SQLite schema and additive migrations, event classification, dashboard queries |
| `scheduler` | which due sources to fetch next; one request per host; host throttling |
| `web` | loopback dashboard and JSON API |
| `app` | the service (poller thread + web server + watchdog) and the command line |

`manage.py` installs/supervises the LaunchAgents and makes private backups; `build_native.py` builds the notification helper from `native/main.swift`; `static/` is the dashboard.

## Verification and limits

Run `python3 -m unittest discover -s tests -v` (stdlib only, about 30 seconds, no network). Tests use local fixtures and HTTP servers and cover every parser kind, URL/time normalisation, polling policy and backoff, bounded fetching (gzip bombs, truncation, timeouts, redirects), event classification, migrations, the web API and its Host/Origin checks, the scheduler, supervision, the installer and an end-to-end feed-to-dashboard run. Seeded property tests feed random and mutated documents, URLs, timestamps and headers to the parsers, normalisers, policy and store. CI runs Python 3.9/3.13 tests plus a macOS native compile/signature check using standard public-repository runners. [GitHub documents standard public-runner usage as free](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Compilation and local fixture tests do not prove operation on a particular Mac mini or notification banner delivery. Each destination needs its own install, permission and live health check. HTTPS certificate validation stays enabled. Public endpoints can disappear, reject requests or change structure; current failures remain visible and retry.

Protocol references: [HTTP cache revalidation](https://www.rfc-editor.org/rfc/rfc9111.html), [compression negotiation](https://www.rfc-editor.org/rfc/rfc9110.html#name-accept-encoding), [Apple user-agent lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html), [Hugging Face public metadata API](https://huggingface.co/docs/hub/en/api).
