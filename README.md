# Job Search Starter

Open this folder in your AI coding agent and say: **"Set up my job search."**
You don't need to fill out a form or run commands. Share a resume in chat or
point the agent to a local file only if you want it to read one.

The agent interviews you, confirms your criteria, and searches for roles.
Then say **"Do a full search for the last seven days"** or **"Find new roles
since the last search."** Works for executive support, marketing, content,
engineering, and other roles; there is no default profession.

## Agent Reference

Read [AGENTS.md](AGENTS.md) for intake and [the operator guide](docs/operator.md)
for features, configuration, and differences between automated and manual
coverage. The [example profile](examples/profile.json) is fictional. Save the
confirmed profile, resumes, reports, and history in ignored `.local/`.

The rotation includes a public source directory, country and remote lanes,
direct ATS boards, FreeHire, public feeds, a Job Radar bridge, multi-ATS web
search, and separate LinkedIn Jobs and public hiring-post routes. It also
includes sponsor-register checks, deduplication, an evidence-backed review
queue, and an optional OpenCode/Kilo discovery worker. The agent completes
manual checks and verifies listings. No scraper provides exhaustive
LinkedIn, Indeed, Glassdoor, or other restricted-site coverage.

```sh
python automation/run_rotation.py --list
python automation/run_rotation.py --broad --days 7
python automation/run_rotation.py --execute --broad --days 7
python -m unittest discover -s tests
```

Python 3.10+ for the core. Job Radar, Brave Search, and external model workers
are optional and require their own setup. No service is contacted in plan-only
mode. The original small `job_search.py` CLI remains a quick-start tool; use
the rotation for full scans. This repository does not apply for jobs or send
messages on the user's behalf.
