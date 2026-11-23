# Selective Upstream Import

Source: https://github.com/ai-job-search-dev/ai-job-search
Revision: 7f01d752516d955a694271f75ee5534bc344873a
Imported: 2026-11-02
License: MIT; copyright and full license retained in LICENSE.

Unmodified imports: tools/posting_guard.py, tools/page_decode.py,
tools/verify_pdf.py. These run offline; no upstream tracker, Gmail,
application submission or LinkedIn automation is enabled.

Local backlog, application lifecycle and Jobbank RSS integration adapt workflow
ideas from the same revision while retaining exact-requisition identities and
the existing local JSON ledger. Upstream automatic expiry after failed fetches
and company/title-only exclusion are intentionally not adopted.

PDF checks inspect text/pages only, not visual layout or an ATS vendor's parser.
Posting checks are heuristics, not a sandbox or proof a posting is trustworthy.
