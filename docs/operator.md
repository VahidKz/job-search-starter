# Full Scan Operator Guide

## Configure For Each Person

Use one clone or workspace per person. The agent conducts the intake and
writes `.local/profile.json` after confirmation. Titles and aliases drive
title matching; skills and configurable fit terms drive discovery ranking.
Hard filters for language, sponsorship, salary, location, work model, and
seniority are verified by the lead, not inferred from keyword rank.

- `selected_lanes`: use named markets from `run_rotation.py --list`, or
  define an arbitrary market under `markets`. Each market may provide its
  location phrases, ISO countries, role queries, search mobility phrases,
  remote-only flag, feeds, sponsor-register codes, and manual sources.
- `boards`: employer, slug, kind, and lanes. Kinds: Ashby (`ashby`),
  Greenhouse (`greenhouse`/`greenhouse_eu`), Lever (`lever`, optional
  `instance: eu`), Factorial, Workable, and BambooHR.
- `disabled_sources`: names or source URLs to omit from manual plans. Edit
  `.local/config/<file>.json` to replace a shipped config privately, including
  the source catalog or board list. Do not commit personal configuration.
- For date boundaries, use the user's local dates. An optional IANA
  `timezone` field needs Python time-zone data available on the system
  (or the `tzdata` package).

## Run And Reconcile

1. Run `python automation/run_rotation.py --execute --broad --days 7`. Without
   `--execute`, this prepares assignments only. Supply repeatable `--lane`
   switches to narrow the scan. `--no-radar` skips the optional collector.
2. Read `coverage.md` and `manifest.json` in the reported run directory.
   Automated collection covers configured direct ATS, FreeHire, public feeds,
   and the Jobbank adapter when access permits. Job Radar uses a bounded,
   shared rotating batch; install explicitly with
   `python automation/job_radar.py install`. ATS web search uses
   `BRAVE_SEARCH_API_KEY` if already configured. Query plans work without it.
3. Execute manual queries using available public browsing. The JSON packs
   distinguish native pages from indexed fallbacks; for LinkedIn Hiring Posts
   they include both indexed post URL families. A disabled, throttled, blocked,
   partially paginated, or sign-in-only surface must stay marked as such.
   The public directory contains both general and specialist sources. Prioritize
   sources relevant to the active profile, then record omissions and why.
   For Relocue, discover URLs from public search, then verify those leads with
   `automation/scan-relocue.ps1 -SeedUrl ...` on Windows or open the employer
   originals directly. No historical shortlist or personal feed is bundled.
4. Read one `review-batch.json` per run. Check employer original, open status,
   source date versus repost/update, and candidate eligibility. Sponsor
   registers are supporting, not role-level evidence. Keep contact-first
   leads separate. Use `review_jobs.py --help` for local decisions or
   `application_workflow.py --help` for archives, outcomes and follow-up
   drafts. Nothing is sent automatically.

## Delegate Discovery

Use browsing workers when available. Assign distinct public queries or one
lane's sources per worker. The lead verifies every returned job and reports
the actual work done, even if a worker returns nothing. Optional external
tools: [OpenCode CLI](https://opencode.ai/docs/cli/) or
[Kilo CLI](https://kilo.ai/docs/code-with-ai/platforms/cli). A free model is
not guaranteed: check availability and price before selecting it. Example:

```sh
python automation/search_worker.py --provider opencode --model provider/model --confirmed --query '"Executive Assistant" London'
```

The worker starts outside the project and receives only the provided query
strings. This is not an OS sandbox: use trusted providers and a read-only
web-search agent configuration. No implicit model or paid fallback. Use the
same assignment and verification procedure in other clients, including
Antigravity; there is no dedicated Antigravity launcher.
