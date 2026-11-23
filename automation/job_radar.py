#!/usr/bin/env python3
"""Pinned Job Radar collector bridge; local history remains authoritative.

No mailbox, application-writing, paid ranking, dashboard, or notification commands
are exposed. Upstream SQLite is a disposable discovery cache, not application state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from settings import config_data
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pipeline import annotate_job, config as search_config, record_jobs, relevant_role_title
from pipeline import annotate_history
from discover_jobs import load_json, save_json
from job_history import review_candidates

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "config" / "job-radar.json"
CACHE = ROOT / ".local" / "cache" / "job-radar"
UPSTREAM = CACHE / "upstream"
PYTHON = CACHE / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DATA = ROOT / ".local" / "state" / "job-radar"
DB = DATA / "discovery.sqlite3"
RUNTIME = DATA / "collector.json"
CURSOR = DATA / "cursor.json"
BATCH = DATA / "batch.json"


@contextmanager
def collector_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "collector.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another Job Radar collection is running") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another Job Radar collection is running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def command(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", errors="replace", check=False)


def revision() -> str:
    result = subprocess.run(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Job Radar checkout unavailable; run job_radar.py install")
    return result.stdout.strip()


def require_engine(settings: dict) -> None:
    if not PYTHON.is_file() or revision() != settings["commit"]:
        raise RuntimeError("Job Radar is missing or differs from the pinned revision; run job_radar.py install")
    dirty = subprocess.run(["git", "-C", str(UPSTREAM), "status", "--porcelain"], capture_output=True, text=True, check=True)
    if dirty.stdout.strip():
        raise RuntimeError("Pinned Job Radar checkout has local changes; review them before collecting")


def install(settings: dict) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    if not UPSTREAM.exists():
        result = command(["git", "clone", settings["repository"], str(UPSTREAM)])
        if result.returncode:
            return result.returncode
    if revision() != settings["commit"]:
        dirty = subprocess.run(["git", "-C", str(UPSTREAM), "status", "--porcelain"], capture_output=True, text=True, check=True)
        if dirty.stdout.strip():
            raise RuntimeError("Upstream checkout has local changes; refusing to replace them")
        result = command(["git", "-C", str(UPSTREAM), "fetch", "origin", settings["commit"]])
        if result.returncode:
            return result.returncode
    result = command(["git", "-C", str(UPSTREAM), "checkout", "--detach", settings["commit"]])
    if result.returncode:
        return result.returncode
    if not PYTHON.exists():
        result = command([sys.executable, "-m", "venv", str(CACHE / "venv")])
        if result.returncode:
            return result.returncode
    result = command([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "config" / "job-radar-requirements.txt")])
    if result.returncode:
        return result.returncode
    result = command([str(PYTHON), "-m", "pip", "install", "--no-deps", "-e", str(UPSTREAM)])
    if not result.returncode:
        frozen = subprocess.run([str(PYTHON), "-m", "pip", "freeze"], capture_output=True, text=True, check=True)
        save_json(CACHE / "installation.json", {"commit": revision(), "dependencies": frozen.stdout.splitlines(), "installed_at": datetime.now(timezone.utc).isoformat()})
    return result.returncode


def catalog(settings: dict) -> dict:
    path = DATA / "sources.json"
    raw = load_json(path if path.exists() else UPSTREAM / "sources" / "sources.json")
    unique = {}
    for row in raw["sources"] + settings.get("extra_sources", []):
        if not isinstance(row, dict):
            raise ValueError("Each source must be an object")
        url = urlparse(row.get("url", ""))
        if row.get("platform") in settings["blocked_platforms"]:
            continue
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Sources must use HTTPS URLs without embedded credentials")
        if not row.get("company") or not row.get("platform"):
            raise ValueError("Each source needs a company and platform")
        unique.setdefault(row["url"], row)
    rows = list(unique.values())
    # Stable mixed-platform ordering makes successive small scans cover new boards.
    rows.sort(key=lambda row: hashlib.sha256(row["url"].encode()).hexdigest())
    return {"meta": raw.get("meta", {}), "sources": rows}


def select_batch(rows: list[dict], limit: int, offset: int) -> tuple[list[dict], int]:
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    if not rows:
        return [], 0
    if not limit or limit >= len(rows):
        return rows, 0
    offset %= len(rows)
    return [rows[(offset + i) % len(rows)] for i in range(limit)], (offset + limit) % len(rows)


def collector_config(settings: dict, rows: list[dict], output: Path) -> dict:
    return {
        "titles": {"include": settings["titles"], "exclude": settings["excluded_titles"]},
        "locations": {"countries": [], "relocate_to": [], "need_sponsorship": [], "remote_ok": True, "work_modes": []},
        "salary": {"floor": None, "currency": "USD"},
        "dealbreakers": [], "sectors": [],
        # Bundled tags include regions such as EU, rejected by upstream's strict
        # user-config country parser. They are employer tags, not hiring gates.
        "sources": {"use_bundled": False, "auto_update": False,
                    "extra": [{key: value for key, value in row.items() if key != "country"} for row in rows]},
        "output": {"formats": ["json"], "dir": str(output)}, "fetch": settings["fetch"],
    }


def upstream(args: list[str], config_path: Path = RUNTIME) -> int:
    # Keep compatibility fixes outside the clean, pinned upstream checkout.
    entry = ROOT / "automation" / "job_radar_runtime.py"
    return command([str(PYTHON), str(entry), "-c", str(config_path), *args], DATA).returncode


def receipt_id(db: Path) -> int:
    if not db.exists():
        return 0
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        return con.execute("SELECT COALESCE(MAX(id),0) FROM source_reads").fetchone()[0]


def role_sources(db: Path) -> dict[str, str]:
    if not db.exists():
        return {}
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        return dict(con.execute("SELECT uid,source_key FROM roles"))


def observations(db: Path, after_id: int = 0) -> dict[str, dict]:
    if not db.exists():
        return {}
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM source_reads WHERE id>? ORDER BY id", (after_id,)).fetchall()
    return {row["source_key"]: dict(row) for row in rows}


def posted_window(value: str | None, days: int, now: datetime) -> str:
    if not isinstance(value, str) or not value:
        return "posting_date_unknown"
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not stamp.tzinfo:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "posting_date_unknown"
    if stamp > now + timedelta(days=1):
        return "posting_date_invalid"
    return "posted_in_window" if stamp >= now - timedelta(days=days) else "older_live_or_cached"


def normalize_roles(payload: dict, settings: dict, health: dict[str, dict], days: int, now: datetime | None = None, sources: dict | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    unique = {}
    for row in payload.get("new", []) + payload.get("matching", []):
        url = urlparse(row.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password or not relevant_role_title(row.get("title", ""), settings):
            continue
        source_key = row.get("source_id") or (sources or {}).get(row.get("uid"), "")
        read = health.get(source_key)
        observation = "not_read_this_run" if not read else ("read_complete" if read["ok"] and read["complete"] else "unknown_or_partial")
        job = annotate_job({
            "source_identity": "job-radar:" + str(row.get("uid") or hashlib.sha256(row["url"].encode()).hexdigest()[:16]),
            "source_portal": row.get("platform"), "source_group": "job_radar",
            "company": row.get("company"), "title": row.get("title"), "url": row.get("url"),
            "location": row.get("location"), "description": row.get("description"),
            "posted_date": row.get("posted_at"), "work_mode": row.get("work_mode"),
            "employment_type": row.get("employment"), "salary": row.get("salary", {}),
            "source_observation": observation, "source_receipt": read,
            "upstream_source_key": source_key, "active_status": "unverified",
            "date_status": posted_window(row.get("posted_at"), days, now),
            "upstream_flags": row.get("flags", []), "status": "discovered_unverified",
            "eligibility_verified": False,
        }, settings)
        # The upstream country can be an employer-board tag; it is not hiring eligibility.
        if job["fit_signal"] == "low":
            job["manual_fit_review"] = True
        unique.setdefault(job["url"], job)
    return list(unique.values())


def health_summary(health: dict, total: int, selected: int, exit_code: int) -> dict:
    complete = sum(bool(row["ok"] and row["complete"]) for row in health.values())
    status = "completed" if not exit_code and complete == selected else "partial"
    if selected < total:
        status = "partial_catalog" if status == "completed" else "partial"
    return {"status": status, "catalog_sources": total, "sources_selected": selected,
            "sources_observed": len(health), "sources_complete": complete, "exit_code": exit_code}


def catalog_stale(meta: dict, settings: dict) -> bool:
    checked = meta.get("checked") or meta.get("validated")
    return posted_window(checked, settings["source_stale_days"], datetime.now(timezone.utc)) != "posted_in_window"


def write_report(path: Path, payload: dict) -> None:
    save_json(path, payload)
    meta = payload["meta"]
    lines = ["# Job Radar Integrated Discovery", "", "> Unverified leads, not applications. Local history is authoritative; no inbox checks. First-seen is not posting date.", "",
             f"Status: `{meta['status']}` | Boards read completely: {meta['sources_complete']}/{meta['sources_selected']} selected, from {meta['catalog_sources']} catalog entries.",
             f"Source catalog stale: `{meta['catalog_stale']}` | Source metadata: `{meta['catalog_meta']}`", ""]
    candidates = review_candidates(payload["results"])
    top = sorted(candidates, key=lambda row: (row.get("source_observation") == "read_complete", row["sort_score"]), reverse=True)[:100]
    lines.extend([f"Review candidates: {len(candidates)}; showing up to100, prioritizing complete board reads. Full JSON and durable queue retain the remainder.",
                  "Already-applied and reviewed/excluded records are not recommendations. Technical scores do not clear eligibility.", ""])
    for heading, fresh in (("Posted In Window", True), ("Older or Undated Backlog", False)):
        lines.extend([f"## {heading}", "", "| Status | Company | Role | Fit | Date | Board evidence | Link |", "|---|---|---|---|---|---|---|"])
        for job in top:
            if (job["date_status"] == "posted_in_window") != fresh:
                continue
            cells = [job["status"], job["company"], job["title"], job["fit_signal"], job["date_status"], job["source_observation"]]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in cells) + f" | [open]({job['url']}) |")
        lines.append("")
    candidate_ids = {id(job) for job in candidates}
    prior = [job for job in payload["results"] if id(job) not in candidate_ids]
    if prior:
        lines.extend(["## Prior Outcomes / Not Recommendations", "", "| Status | Company | Role | Fit | Date | Board evidence | Link |", "|---|---|---|---|---|---|---|"])
        for job in prior[:30]:
            cells = [job["status"], job["company"], job["title"], job["fit_signal"], job["date_status"], job["source_observation"]]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in cells) + f" | [open]({job['url']}) |")
    lines.extend(["", "## Coverage Failures", ""])
    lines.extend(f"- {row['source']}: {row['error']}" for row in meta["failures"])
    lines.extend(["", "Roles with unknown/old dates are retained as a separate backlog, not called new postings. Confirm original vacancy and location and sponsorship before recommending.", ""])
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def export_cache(settings: dict, args) -> int:
    if not DB.is_file():
        raise RuntimeError("No discovery cache exists; scan or explicitly load a seed first")
    script = "from jobradar import store; import json, sys\nwith store.open_db(sys.argv[1]) as con:\n print(json.dumps({'matching': [job.to_dict() for job in store.live_jobs(con)]}))"
    result = subprocess.run([str(PYTHON), "-c", script, str(DB)], cwd=DATA, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=True)
    roles = normalize_roles(json.loads(result.stdout), search_config(), {}, args.days, sources=role_sources(DB))
    counts = (annotate_history(roles, "job_radar") if args.no_state else record_jobs(roles, "job_radar")) if roles else {}
    source_catalog = catalog(settings)
    meta = {"status": "cache_only_not_scanned", "sources_complete": 0, "sources_selected": 0,
            "catalog_sources": len(source_catalog["sources"]), "catalog_stale": catalog_stale(source_catalog["meta"], settings),
            "catalog_meta": source_catalog["meta"], "failures": [], "results": len(roles), "deduplication": counts,
            "days": args.days, "created_at": datetime.now(timezone.utc).isoformat(), "upstream_commit": settings["commit"]}
    write_report(args.output, {"meta": meta, "results": roles, "source_health": {}})
    print(f"Cache import: {len(roles)} unverified leads; no live boards scanned")
    print(f"Raw data: {args.output}")
    print(f"Report: {args.output.with_suffix('.md')}")
    return 0


def run_scan(settings: dict, args) -> int:
    require_engine(settings)
    DATA.mkdir(parents=True, exist_ok=True)
    source_catalog = catalog(settings)
    fingerprint = hashlib.sha256(json.dumps(source_catalog["sources"], sort_keys=True).encode()).hexdigest()
    if args.resume:
        batch = load_json(BATCH)
        if batch["fingerprint"] != fingerprint or not RUNTIME.exists():
            raise RuntimeError("Catalog changed or collector config missing; resume refused")
        if hashlib.sha256(RUNTIME.read_bytes()).hexdigest() != batch["config_hash"]:
            raise RuntimeError("Collector configuration changed; resume refused")
        output_dir = Path(batch["output_dir"])
    else:
        cursor = load_json(CURSOR) if CURSOR.exists() else {}
        offset = cursor.get("offset", 0) if cursor.get("fingerprint") == fingerprint else 0
        rows = source_catalog["sources"]
        if args.company:
            wanted = {name.casefold() for name in args.company}
            rows = [row for row in rows if row["company"].casefold() in wanted]
        selected, next_offset = select_batch(rows, args.limit, offset)
        if not selected:
            raise RuntimeError("No employer boards matched; nothing was scanned")
        output_dir = args.output.parent / (args.output.stem + "-native")
        batch = {"fingerprint": fingerprint, "selected": len(selected), "next_offset": next_offset,
                 "output_dir": str(output_dir), "advance_cursor": not bool(args.company)}
        save_json(RUNTIME, collector_config(settings, selected, output_dir))
        batch["config_hash"] = hashlib.sha256(RUNTIME.read_bytes()).hexdigest()
        save_json(BATCH, batch)
    cli = ["scan", "--db", str(DB), "--state", str(DATA / "seen.json"), "--no-open", "--no-caffeine", "--no-source-update"]
    if args.resume:
        cli.append("--resume")
    if args.no_enrich:
        cli.append("--no-enrich")
    before_id = receipt_id(DB)
    previous_report = native_stamp = None
    native = output_dir / "roles.json"
    if native.exists():
        previous_report = native.stat().st_mtime_ns
    exit_code = upstream(cli)
    health = observations(DB, before_id)
    if native.exists():
        native_stamp = native.stat().st_mtime_ns
    report_missing = not exit_code and (native_stamp is None or native_stamp == previous_report)
    if report_missing:
        exit_code = 2
    snapshot = load_json(native) if not exit_code else {}
    actual_sources = snapshot.get("meta", {}).get("sources_total", batch["selected"])
    summary = health_summary(health, max(len(source_catalog["sources"]), actual_sources), actual_sources, exit_code)
    roles = normalize_roles(snapshot, search_config(), health, args.days, sources=role_sources(DB))
    # Keep failed/unvisited cached boards visible, but never mark their jobs verified open.
    failures = [{"source": key, "error": row.get("why") or "board unread or pagination incomplete"}
                for key, row in health.items() if not row["ok"] or not row["complete"]]
    runtime_sources = load_json(RUNTIME)["sources"]["extra"]
    failures.extend({"source": row["url"], "error": "No durable parser receipt in this run; empty/unread status is unconfirmed"}
                    for row in runtime_sources if not row.get("keyword_template") and row["url"].split("#", 1)[0] not in health)
    counts = (annotate_history(roles, "job_radar") if args.no_state else record_jobs(roles, "job_radar")) if roles else {}
    if report_missing:
        summary["status"] = "report_missing"
    summary.update({"created_at": datetime.now(timezone.utc).isoformat(), "source_group": "job_radar",
                    "upstream_commit": settings["commit"], "catalog_meta": source_catalog["meta"], "catalog_stale": catalog_stale(source_catalog["meta"], settings),
                    "catalog_entries_selected": batch["selected"], "failures": failures, "results": len(roles), "deduplication": counts, "days": args.days})
    write_report(args.output, {"meta": summary, "results": roles, "source_health": health})
    if not exit_code and batch["advance_cursor"]:
        save_json(CURSOR, {"fingerprint": fingerprint, "offset": batch["next_offset"]})
    print(f"Job Radar: {summary['status']} | Complete boards: {summary['sources_complete']}/{batch['selected']} | Leads: {len(roles)}")
    print(f"Raw data: {args.output}")
    print(f"Report: {args.output.with_suffix('.md')}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install")
    sub.add_parser("catalog")
    sub.add_parser("refresh-sources")
    scan = sub.add_parser("scan")
    scan.add_argument("--limit", type=int, default=None, help="Rotating board batch; 0 scans all catalog sources")
    scan.add_argument("--company", action="append", default=[])
    scan.add_argument("--days", type=int, default=3)
    scan.add_argument("--resume", action="store_true")
    scan.add_argument("--no-state", action="store_true")
    scan.add_argument("--no-enrich", action="store_true")
    scan.add_argument("--output", type=Path)
    export = sub.add_parser("export", help="Import cached/seed/enriched leads without claiming a live scan")
    export.add_argument("--days", type=int, default=3)
    export.add_argument("--no-state", action="store_true")
    export.add_argument("--output", type=Path)
    for action in ("doctor", "coverage", "rescreen", "closures"):
        sub.add_parser(action)
    for action in ("validate", "enrich"):
        bounded = sub.add_parser(action)
        bounded.add_argument("--limit", type=int, default=None, help="Default 200; 0 explicitly requests all")
    discover = sub.add_parser("discover")
    discover.add_argument("targets", nargs="+")
    seed = sub.add_parser("seed")
    seed.add_argument("path", help="Explicit seed directory or HTTPS release URL; never treated as a live scan")
    args = parser.parse_args()
    settings = config_data(SETTINGS.name, require_profile=args.action in {"scan", "export"})
    try:
        if args.action == "install":
            with collector_lock():
                return install(settings)
        require_engine(settings)
        DATA.mkdir(parents=True, exist_ok=True)
        if args.action == "refresh-sources":
            url = f"https://raw.githubusercontent.com/maccydee/job-radar/main/sources/sources.json"
            with urlopen(Request(url, headers={"User-Agent": "job-search-starter/1.0"}), timeout=45) as response:
                fresh = json.load(response)
            if not isinstance(fresh.get("sources"), list) or not fresh["sources"]:
                raise ValueError("Invalid source catalog; existing catalog retained")
            with collector_lock():
                save_json(DATA / "sources.json", fresh)
            print(f"Source catalog refreshed: {len(fresh['sources'])} entries; board validation is still required")
            return 0
        if args.action == "catalog":
            data = catalog(settings)
            print(json.dumps({"sources": len(data["sources"]), "platforms": sorted({row['platform'] for row in data['sources']}), "meta": data["meta"]}, indent=2))
            return 0
        if args.action in {"scan", "export"}:
            if args.action == "scan":
                args.limit = settings["default_batch_size"] if args.limit is None else args.limit
            if getattr(args, "limit", 0) < 0 or args.days < 1:
                parser.error("limit must be nonnegative and days must be positive")
            args.output = (args.output or ROOT / ".local" / "state" / "runs" / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%SZ}-job-radar-global.json").resolve()
            with collector_lock():
                return run_scan(settings, args) if args.action == "scan" else export_cache(settings, args)
        with collector_lock():
            maintenance = DATA / "maintenance.json"
            save_json(maintenance, collector_config(settings, catalog(settings)["sources"], DATA / "out"))
            if args.action == "discover":
                return upstream(["discover", *args.targets], maintenance)
            if args.action == "seed":
                return upstream(["seed", "load", args.path, "--keep", str(DATA / "seed"), "--db", str(DB)], maintenance)
            if args.action == "coverage":
                return upstream([args.action], maintenance)
            if args.action in {"validate", "enrich"}:
                limit = settings["default_batch_size"] if args.limit is None else args.limit
                if limit < 0:
                    parser.error("limit must be nonnegative")
                extra = ["--report", str(DATA / "validation.json"), "--state-dir", str(DATA / "validation")] if args.action == "validate" else ["--db", str(DB)]
                return upstream([args.action, "--limit", str(limit), *extra], maintenance)
            return upstream([args.action, "--db", str(DB)], maintenance)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"Job Radar integration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
