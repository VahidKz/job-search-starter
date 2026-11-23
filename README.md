# Job Search Starter

Open this folder in your AI coding agent and say: **"Set up my job search."**
You don't need to fill out a form or run commands. Share a resume in chat or
point the agent to a local file only if you want it to read one.

The agent interviews you, confirms your criteria, and searches for roles.
You can then say **"Find new roles since the last search"** or **"Check this
posting against my profile."** No accounts, API keys, or paid services are
required by the included tools. Live web search requires an agent with web
access or access to supported public employer boards.

## Agent Reference

Read [AGENTS.md](AGENTS.md) for the interview, verification and reporting
workflow. The example [profile](examples/profile.json) is fictional. Save
confirmed settings to `.local/profile.json`, not over the example.
The repository supports any job family: no built-in engineering, marketing
or geography preference. Public inputs are only search keywords and employer
board identifiers. Private resumes, preferences, and history stay in
`.local/` or outside the repo; the CLI never loads a resume.

Python 3.10+; standard library only:

```sh
python job_search.py plan
python job_search.py scan --days 14
python -m unittest discover -s tests
```

`plan` emits plain search queries for the agent's web tool. `scan` checks
user-selected public Ashby, Greenhouse and Lever employer boards. Configure
their board slugs under `boards` after finding official careers pages. Save
results with `--output .local/leads.json` or read them from stdout. Board
results are **unverified leads**; location, salary, language, and permission
to work are never inferred by the CLI. An empty board list is reported as
not configured, not as zero jobs in the market.

This is a standalone starter, not a hosted job board, application service,
or a complete clone of any personal workspace.
