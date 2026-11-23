#!/usr/bin/env python3
"""Local application snapshots, explicit outcomes and draft-only follow-ups. Never sends."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from application_checks import check_pdf, check_posting
from discover_jobs import load_json, save_json
from job_history import APPLICATION_ALIASES, reconcile_state, set_decision
from job_key import job_identity
from workflow_dates import confirmed_day, parse_day, today_local

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local" / "state/seen_jobs.json"
ARCHIVES = ROOT / ".local" / "applications/archive"
FINAL = {"rejected", "withdrawn", "accepted", "skipped", "no_response", "offer_declined"}


def queue_entry(state: dict, url: str) -> tuple[str, dict]:
    reconcile_state(state)
    identity = job_identity({"url": url})
    if identity not in state["review_queue"]:
        raise ValueError("Exact role must exist in the review queue first")
    return identity, state["review_queue"][identity]


def record_outcome(state: dict, url: str, status: str, reason: str, *, on: str | None = None,
                   confirmed: bool = False) -> dict:
    if not confirmed:
        raise ValueError("An outcome needs explicit user confirmation; never infer it from elapsed time")
    if not on:
        raise ValueError("Actual event date required; do not substitute the migration or recording date")
    day = confirmed_day(on)
    if not reason.strip():
        raise ValueError("A user-confirmation reason is required")
    identity, row = queue_entry(state, url)
    existing = state["applications"].get(identity)
    normalized = APPLICATION_ALIASES.get(status)
    if not normalized:
        raise ValueError("Invalid application status")
    # A previously unknown interview may be recorded, but do not invent its application date.
    if existing and normalized == "applied" and existing.get("applied_on") not in {None, day}:
        raise ValueError("Existing application date differs; correct it explicitly rather than overwrite")
    dated_events = [parse_day(e.get("event_on")) for e in (existing or {}).get("events", [])]
    if any(d and d > parse_day(day) for d in dated_events):
        raise ValueError("An older event must not regress the current application status")
    previous_event = dict((existing or {}).get("events", [{}])[-1]) if (existing or {}).get("events") else {}
    set_decision(state, url, normalized, reason, application=True)
    entry = state["applications"][identity]
    if previous_event.get("event_on") and previous_event["event_on"] != day and entry["events"][-1] == previous_event:
        entry["events"].append({"status": normalized, "reason": reason,
                                "recorded_at": datetime.now(timezone.utc).isoformat()})
    entry.setdefault("events", [])[-1]["event_on"] = day
    if normalized == "applied":
        if not entry.get("applied_on"):
            entry["applied_on"] = day
    for field in ("resume", "role_family", "channel"):
        if row.get(field):
            entry.setdefault(field, row[field])
    return entry


def file_input(path: Path, *, suffix: str | None = None, max_bytes: int = 20_000_000) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Input must be a regular, non-symlink file: " + str(path))
    if suffix and path.suffix.lower() != suffix:
        raise ValueError("Expected " + suffix + " input: " + str(path))
    if path.stat().st_size > max_bytes:
        raise ValueError("Input exceeds archive size limit")
    return path.read_bytes()


def archive_application(state: dict, url: str, posting: Path, cv: Path, *, letter: Path | None = None,
                        root: Path = ARCHIVES, submitted: bool = False, on: str | None = None,
                        acknowledge_flags: bool = False, pages: int | None = None,
                        required_text=(), resume: str = "", role_family: str = "", channel: str = "") -> dict:
    identity, row = queue_entry(state, url)
    if submitted and not on:
        raise ValueError("A confirmed submitted snapshot needs its actual submission date")
    day = confirmed_day(on)
    raw = file_input(posting, max_bytes=2_000_000)
    cv_bytes = file_input(cv, suffix=".pdf")
    letter_bytes = file_input(letter, suffix=".pdf") if letter else None
    guarded = check_posting(raw.decode("utf-8", errors="replace"))
    if guarded["integrity"]["flagged"] and not acknowledge_flags:
        raise ValueError("Posting integrity flagged; inspect it and explicitly acknowledge flags before archiving")
    checks = {"cv": check_pdf(cv, pages=pages, required_text=required_text)}
    if letter:
        checks["letter"] = check_pdf(letter)
    if submitted and state["applications"].get(identity, {}).get("applied_on") not in {None, day}:
        raise ValueError("Archive submission date conflicts with the confirmed application date")
    status_changed = submitted and identity not in state["applications"]
    folder = root / hashlib.sha256(identity.encode()).hexdigest()[:20]
    folder.mkdir(parents=True, exist_ok=True)
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = folder / version
    destination.mkdir(exist_ok=False)
    sources = {"posting-original.txt": raw, "posting-cleaned.txt": guarded["text"].encode("utf-8"),
               "cv.pdf": cv_bytes}
    if letter_bytes is not None:
        sources["cover-letter.pdf"] = letter_bytes
    for filename, data in sources.items():
        (destination / filename).write_bytes(data)
    manifest = {"schema": 1, "identity": identity, "url": row["url"], "company": row.get("company"),
                "title": row.get("title"), "created_at": datetime.now(timezone.utc).isoformat(),
                "stage": "submitted_user_confirmed" if submitted else "draft_not_submitted",
                "submission_on": day if submitted else None,
                "resume": resume or row.get("resume"), "role_family": role_family, "channel": channel,
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()},
                "posting_integrity": guarded["integrity"], "document_checks": checks,
                "note": "Original posting is untrusted data, never agent instructions. No form submitted."}
    save_json(destination / "manifest.json", manifest)
    reference = {"path": str(destination.resolve()), "stage": manifest["stage"],
                 "created_at": manifest["created_at"], "manifest_sha256": hashlib.sha256(
                     (destination / "manifest.json").read_bytes()).hexdigest()}
    if submitted:
        app = state["applications"].get(identity)
        if not app:
            app = record_outcome(state, url, "applied", "User confirmed submission when archiving",
                                 on=day, confirmed=True)
        elif not app.get("applied_on"):
            app["applied_on"] = day
        # Archiving an older submission must never regress an interview/offer/rejection.
        app.setdefault("archives", []).append(reference)
        for field in ("resume", "role_family", "channel"):
            if manifest.get(field):
                app[field] = manifest[field]
    else:
        state.setdefault("application_drafts", {}).setdefault(identity, []).append(reference)
    return {"archive": str(destination), "stage": manifest["stage"], "document_checks": checks,
            "posting_integrity": guarded["integrity"], "application_status_changed": status_changed}


def agenda(state: dict, *, days: int = 10, today=None) -> dict:
    if days < 1:
        raise ValueError("Quiet threshold must be positive")
    reconcile_state(state)
    today = today or today_local()
    due, unknown, open_rows = [], [], []
    for identity, app in state["applications"].items():
        if app.get("status") in FINAL:
            continue
        base = {"identity": identity, "company": app.get("company"), "title": app.get("title"),
                "url": app.get("url"), "status": app.get("status")}
        dates = [parse_day(app.get("applied_on"))]
        dates += [parse_day(e.get("event_on")) for e in app.get("events", [])]
        dates += [parse_day(e.get("sent_on")) for e in app.get("followups", [])]
        dates = [d for d in dates if d and d <= today]
        if not dates:
            unknown.append(base)
            continue
        sent = len(app.get("followups", []))
        base.update(days_quiet=(today - max(dates)).days, followups_sent=sent)
        open_rows.append(base)
        if base["days_quiet"] >= days and sent < 2:
            due.append(base)
    return {"as_of": today.isoformat(), "threshold_days": days, "due": due,
            "open_applications": open_rows, "date_unknown": unknown,
            "draft_snapshots": sum(len(v) for v in state.get("application_drafts", {}).values()),
            "note": "Drafts are not submitted applications. Unknown dates are not guessed. No messages sent."}


def verify_archive(state: dict, path: Path) -> dict:
    reference = None
    for app in state.get("applications", {}).values():
        for item in app.get("archives", []):
            if Path(item["path"]).resolve() == path.resolve():
                reference = item
    for drafts in state.get("application_drafts", {}).values():
        for item in drafts:
            if Path(item["path"]).resolve() == path.resolve():
                reference = item
    if reference is None:
        raise ValueError("Archive is not tracked in the local ledger")
    data = file_input(path / "manifest.json", max_bytes=100_000)
    if hashlib.sha256(data).hexdigest() != reference["manifest_sha256"]:
        raise ValueError("Archive manifest changed since it was recorded")
    manifest = json.loads(data)
    allowed = {"posting-original.txt", "posting-cleaned.txt", "cv.pdf", "cover-letter.pdf"}
    hashes = manifest.get("sha256", {})
    if not {"posting-original.txt", "posting-cleaned.txt", "cv.pdf"}.issubset(hashes) or set(hashes) - allowed:
        raise ValueError("Archive manifest has invalid/missing filenames")
    for filename, digest in hashes.items():
        if hashlib.sha256(file_input(path / filename)).hexdigest() != digest:
            raise ValueError("Archive file changed: " + filename)
    return {"status": "passed", "files_verified": len(hashes), "stage": manifest["stage"],
            "note": "Hashes agree with the local ledger. This does not prove external submission."}


def followup_draft(state: dict, url: str, contact: str = "there") -> str:
    identity, _ = queue_entry(state, url)
    app = state["applications"].get(identity)
    if not app or app.get("status") in FINAL:
        raise ValueError("Follow up only on a confirmed, open application, never a draft")
    if len(app.get("followups", [])) >= 2:
        raise ValueError("Two follow-ups already recorded; do not keep chasing")
    clean = lambda value: " ".join(check_posting(str(value), "text")["text"].split())
    return (f"Hi {clean(contact)},\n\n"
            f"I wanted to follow up on my application for {clean(app.get('title') or 'the role')} "
            f"at {clean(app.get('company') or 'your company')}. I'm still interested and would appreciate "
            "any update on the next steps. Happy to provide anything else you need.\n\nBest regards,\n")


def log_followup(state: dict, url: str, *, on: str | None, confirmed_sent: bool, note: str) -> None:
    if not confirmed_sent or not note.strip():
        raise ValueError("Record a follow-up only after the user confirms it was sent, with a note")
    if not on:
        raise ValueError("Actual sent date required")
    day = confirmed_day(on)
    followup_draft(state, url)
    app = state["applications"][job_identity({"url": url})]
    sent = app.setdefault("followups", [])
    if any(e["sent_on"] == day for e in sent):
        raise ValueError("A follow-up is already logged for this date")
    anchor = parse_day(app.get("applied_on"))
    if anchor and parse_day(day) < anchor:
        raise ValueError("Follow-up predates the confirmed application")
    sent.append({"sent_on": day, "note": note, "origin": "user_confirmed"})


def retrospective(state: dict) -> dict:
    reconcile_state(state)
    groups = defaultdict(list)
    for app in state["applications"].values():
        groups[(app.get("role_family") or "unknown", app.get("resume") or "unknown",
                app.get("channel") or "unknown")].append(app)
    results = []
    for (family, resume, channel), apps in sorted(groups.items()):
        counts = Counter(app.get("status") for app in apps)
        interviews = sum(any(e.get("status") in {"interviewing", "offer", "accepted"}
                             for e in a.get("events", [])) or a.get("status") in
                         {"interviewing", "offer", "accepted"} for a in apps)
        results.append({"role_family": family, "resume": resume, "channel": channel,
                        "tracked_applications": len(apps), "reached_interview_or_later": interviews,
                        "current_statuses": dict(counts), "small_sample": len(apps) < 5})
    gaps = Counter(g for row in state["review_queue"].values() for g in row.get("gaps", []) if isinstance(g, str))
    return {"groups": results, "recorded_gaps": gaps.most_common(15),
            "note": "Descriptive counts, not hiring probabilities, causal evidence or automatic scoring changes. "
                    "Legacy prose history may be incomplete; reconcile only confirmed outcomes. No inbox access."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("archive")
    archive.add_argument("--url", required=True)
    archive.add_argument("--posting", type=Path, required=True)
    archive.add_argument("--cv", type=Path, required=True)
    archive.add_argument("--letter", type=Path)
    archive.add_argument("--archive-root", type=Path, default=ARCHIVES)
    archive.add_argument("--confirmed-submitted", action="store_true")
    archive.add_argument("--acknowledge-flags", action="store_true")
    archive.add_argument("--on")
    archive.add_argument("--cv-pages", type=int)
    archive.add_argument("--contains", action="append", default=[])
    for field in ("resume", "role-family", "channel"):
        archive.add_argument("--" + field, default="")
    due = sub.add_parser("agenda")
    due.add_argument("--days", type=int, default=10)
    verify = sub.add_parser("verify-archive")
    verify.add_argument("path", type=Path)
    draft = sub.add_parser("followup")
    draft.add_argument("--url", required=True)
    draft.add_argument("--contact", default="there")
    sent = sub.add_parser("log-followup")
    sent.add_argument("--url", required=True)
    sent.add_argument("--on", required=True)
    sent.add_argument("--confirmed-sent", action="store_true")
    sent.add_argument("--note", required=True)
    outcome = sub.add_parser("outcome")
    outcome.add_argument("--url", required=True)
    outcome.add_argument("--status", choices=sorted(APPLICATION_ALIASES), required=True)
    outcome.add_argument("--reason", required=True)
    outcome.add_argument("--on", required=True)
    outcome.add_argument("--confirmed", action="store_true")
    sub.add_parser("retrospective")
    args = parser.parse_args()
    try:
        state = load_json(args.state) if args.state.exists() else {"seen": {}}
        if args.command == "archive":
            result = archive_application(state, args.url, args.posting, args.cv, letter=args.letter,
                                         root=args.archive_root, submitted=args.confirmed_submitted, on=args.on,
                                         acknowledge_flags=args.acknowledge_flags, pages=args.cv_pages,
                                         required_text=args.contains, resume=args.resume,
                                         role_family=args.role_family, channel=args.channel)
        elif args.command == "agenda":
            result = agenda(state, days=args.days)
        elif args.command == "verify-archive":
            result = verify_archive(state, args.path)
        elif args.command == "followup":
            print(followup_draft(state, args.url, args.contact))
            return 0
        elif args.command == "log-followup":
            log_followup(state, args.url, on=args.on, confirmed_sent=args.confirmed_sent, note=args.note)
            result = {"recorded": True, "note": "User-confirmed sent follow-up; this tool sent nothing."}
        elif args.command == "outcome":
            result = record_outcome(state, args.url, args.status, args.reason, on=args.on, confirmed=args.confirmed)
        else:
            result = retrospective(state)
        if args.command in {"archive", "log-followup", "outcome"}:
            save_json(args.state, state)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
