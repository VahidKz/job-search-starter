"""Stable keys for cross-run job deduplication.

Adapted from MadsLorentzen/ai-job-search tools/job_key.py under the MIT license.
The upstream revision is recorded in ../third-party/ai-job-search.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

COMPANY_MAX = 40
TITLE_MAX = 60
HASH_LEN = 6
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_JOB_ID = re.compile(r"(\d{6,})")
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer", "source", "trk"}


def canonical_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value.strip())
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/",
                       "", urlencode(sorted(query)), ""))


def job_identity(job: dict) -> str:
    """Prefer exact ATS requisitions; never merge different jobs by title alone."""
    url = canonical_url(str(job.get("url") or ""))
    parsed = urlparse(url)
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.split("/") if part]
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return f"ashby:{parts[0].lower()}:{parts[1]}"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        return f"lever:{parts[0].lower()}:{parts[1]}"
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}:
        if len(parts) >= 3 and parts[1] == "jobs":
            return f"greenhouse:{parts[0].lower()}:{parts[2]}"
    if host == "apply.workable.com" and "j" in parts:
        index = parts.index("j")
        if index + 1 < len(parts):
            return f"workable:{parts[index + 1]}"
    if host.endswith(".bamboohr.com") and len(parts) >= 2 and parts[0] == "careers":
        return f"bamboohr:{host.split('.')[0]}:{parts[1]}"
    return url or str(job.get("source_identity") or job.get("key") or "")


def slugify(text: str) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_only.lower()).strip("-")


def _cap(slug: str, limit: int) -> str:
    if len(slug) <= limit:
        return slug
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:HASH_LEN]
    return f"{slug[:limit].rstrip('-')}-{digest}"


def make_key(company: str, title: str, url: str = "") -> str:
    company_slug = _cap(slugify(company), COMPANY_MAX) or "unknown-company"
    title_slug = _cap(slugify(title), TITLE_MAX)
    if not title_slug:
        match = _JOB_ID.search(url or "")
        if match:
            title_slug = match.group(1)
        else:
            digest = hashlib.sha1((str(title) + str(url)).encode("utf-8")).hexdigest()[:HASH_LEN]
            title_slug = f"untitled-{digest}"
    return f"{company_slug}_{title_slug}"
