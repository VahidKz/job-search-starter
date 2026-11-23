"""Offline posting integrity and PDF text checks using pinned MIT upstream tools."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from functools import cache
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "third-party/ai-job-search/2026-11-02/tools"


@cache
def upstream(name: str):
    if name not in {"posting_guard", "verify_pdf"}:
        raise ValueError("Unsupported upstream tool")
    spec = importlib.util.spec_from_file_location("job_search_upstream_" + name, VENDOR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_posting(raw: str, mode: str = "auto") -> dict:
    text, findings = upstream("posting_guard").guard(raw, mode)
    # Keep payload excerpts out of durable records; the agent sees cleaned data only.
    summary = {
        "verdict": findings["verdict"],
        "flagged": "review before continuing" in findings["verdict"],
        "hidden_blocks": len(findings.get("hidden_blocks", [])),
        "hidden_urls": len(findings.get("hidden_urls", [])),
        "instruction_like_lines": len(findings.get("instruction_like", [])),
        "invisible_characters": findings.get("invisible_chars", {}),
        "note": "Heuristic inspection, not a sandbox, employer verification or eligibility proof.",
    }
    return {"text": text, "integrity": summary}


def check_pdf(path: Path, pages: int | None = None, required_text=(), min_chars: int = 100) -> dict:
    if pages is not None and pages < 1:
        raise ValueError("Expected PDF page count must be positive")
    if min_chars < 1:
        raise ValueError("Minimum text length must be positive")
    extractor, text, count = upstream("verify_pdf").verify_pdf(
        path, expected_pages=pages, min_chars=min_chars, required_text=required_text,
    )
    return {"status": "passed", "pages": count, "text_characters": len(text), "extractor": extractor,
            "required_text_checked": list(required_text),
            "note": "Text/page checks only. Visual layout and ATS-vendor compatibility require separate review."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    posting = sub.add_parser("posting")
    posting.add_argument("file", type=Path)
    posting.add_argument("--mode", choices=["auto", "html", "text"], default="auto")
    pdf = sub.add_parser("pdf")
    pdf.add_argument("file", type=Path)
    pdf.add_argument("--pages", type=int)
    pdf.add_argument("--contains", action="append", default=[])
    pdf.add_argument("--min-chars", type=int, default=100)
    args = parser.parse_args()
    try:
        if args.command == "posting":
            result = check_posting(args.file.read_text(encoding="utf-8"), args.mode)
        else:
            result = check_pdf(args.file, args.pages, args.contains, args.min_chars)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
