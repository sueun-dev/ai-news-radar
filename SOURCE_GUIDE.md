# Public source catalog

The default catalog contains 203 public collection paths across 102 hosts, originally checked on 2026-10-19. A path is not a unique publisher, a verified news event, or an assurance of future availability. Use the dashboard's latest success/error state for current health.

| Group | Paths |
|---|---:|
| Official | 31 |
| Models/repositories | 29 |
| Tools/product blogs | 25 |
| Open-source releases | 40 |
| Media/analysis | 26 |
| Korean sources | 12 |
| Research | 16 |
| Communities | 4 |
| Multilingual news queries | 18 |
| Policy | 2 |

`SOURCES.csv` is the full URL/interval list. OPML includes 149 RSS/Atom feed paths; it does not represent the HTML, sitemap or model metadata parsers.

Supported `kind` values in config:

- `feed`: RSS/Atom/RDF; source-specific `time_basis` can distinguish indexing or batch timestamps. `allow_empty` is only for a valid feed root with no current entries.
- `links`: source-specific article URL regex (`link_pattern`) applied to a public HTML list. Navigation is excluded by default. Titleless paths are labeled explicitly.
- `sitemap`: URL discovery with `link_pattern`. `lastmod` is not a publication time. These paths normally store metadata without alerts.
- `hf_models`: unauthenticated public model metadata scoped to `author`, without inference or model downloads. Repository creation is not verified product release time.
- `anthropic`, `changelog`: existing source-specific public-list parsers.

Default intervals are 5–30 minutes. All source IDs must be unique. `notify` sets the initial alert preference; per-source dashboard choices override it. `detect_updates: false` prevents changing scores/popularity counters from appearing as news. General technology feeds can use `filter_ai`; this keyword filter has false positives/negatives. `notify_prerelease` controls prerelease alerts. Reinstall after changing configuration and preserve backups.

This is finite current-list polling, not unlimited history crawling. Feed caps and large batches can hide entries between checks. Google News queries span eight languages but are limited by indexing and exposed windows. Local copies retain headline/link/time/hash metadata rather than full articles, paid text, PDFs or weights.

Private channels, login-only or paid article text, personal email subscriptions and all X/Threads/LinkedIn posts are outside scope. Access denials and rate limits are not bypassed. `coverage.json` records excluded candidates and reasons from catalog discovery; some providers are covered through a different working path. Every claim from communities, independent blogs or preprints still requires its own verification.
