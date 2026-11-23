#!/usr/bin/env python3
"""Run a bounded health probe against the configured FreeHire source."""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlencode, urlparse

from discover_jobs import fetch_json, repair_text


def main() -> int:
    base = os.environ.get("FREEHIRE_API_URL", "https://freehire.me").rstrip("/")
    params = urlencode({"q": "software engineer", "limit": 3, "posted_within_days": 14})
    try:
        payload = fetch_json(f"{base}/api/v1/agent/jobs/search?{params}")
        rows = payload.get("data", [])
        problems: list[str] = []
        if not rows:
            problems.append("sentinel query returned zero results")
        for index, row in enumerate(rows):
            if not repair_text(row.get("title")):
                problems.append(f"result {index} has no title")
            if not repair_text(row.get("company")):
                problems.append(f"result {index} has no company")
            if not urlparse(repair_text(row.get("url"))).netloc:
                problems.append(f"result {index} has no usable URL")
        verdict = "healthy" if not problems else "degraded"
        print(json.dumps({"portal": "freehire", "verdict": verdict, "results": len(rows), "problems": problems}, indent=2))
        return 0 if verdict == "healthy" else 1
    except Exception as exc:
        print(json.dumps({"portal": "freehire", "verdict": "broken_or_unreachable", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
