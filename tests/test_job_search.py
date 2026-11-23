import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

from job_search import load_profile, plan, scan


NOW = datetime(2026, 1, 12, tzinfo=timezone.utc)
RECENT = "2026-01-11T09:00:00Z"
BASE = "https://example.org/careers/"


class JobSearchTests(unittest.TestCase):
    def test_intake_confirmation_and_board_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = {"confirmed": False, "target_titles": ["Executive Assistant"]}
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Confirm"):
                load_profile(path)
            profile["confirmed"] = True
            path.write_text(json.dumps(profile), encoding="utf-8")
            self.assertEqual(load_profile(path), profile)
            profile["boards"] = [{"platform": "ashby", "slug": "../elsewhere"}]
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "board slugs"):
                load_profile(path)

    def test_plan_only_uses_public_search_fields(self):
        queries = plan({
            "target_titles": ["Executive Assistant"],
            "search_terms": ["executive support"],
            "search_locations": ["London"],
            "hard_constraints": ["test-private-constraint"],
            "evidence": ["test-private-evidence"],
        })
        self.assertIn('"Executive Assistant" "London"', queries)
        self.assertIn('site:jobs.ashbyhq.com "Executive Assistant" "London"', queries)
        self.assertIn('"executive support" jobs "London"', queries)
        self.assertNotIn("test-private", " ".join(queries))

    def test_scan_matches_three_job_families_and_preserves_date_meaning(self):
        assistant = {"id": 1, "title": "Senior Executive Assistant", "jobUrl": BASE + "assistant",
                     "publishedAt": RECENT, "location": "Remote"}
        marketing = {"id": 2, "title": "Marketing Specialist", "absolute_url": BASE + "marketing",
                     "updated_at": RECENT, "location": {"name": "London"}}
        creator = {"id": "3", "text": "Content Creator", "hostedUrl": BASE + "creator",
                   "createdAt": int(NOW.timestamp() * 1000), "categories": {"location": "Paris"}}
        feeds = {
            "ashby": {"jobs": [
                assistant, assistant,
                {**assistant, "id": 4, "publishedAt": "2025-12-01T00:00:00Z"},
                {**assistant, "title": "Software Engineer"},
                {**assistant, "id": 5, "jobUrl": "https://[bad"},
            ]},
            "greenhouse": {"jobs": [marketing]},
            "lever": [creator, {**creator, "id": "6", "createdAt": None}],
        }
        profile = {
            "target_titles": ["Executive Assistant", "Marketing Specialist", "Content Creator"],
            "boards": [{"platform": platform, "slug": "example"} for platform in feeds],
        }
        result = scan(profile, 7, fetch=lambda platform, slug: feeds[platform], now=NOW)
        self.assertEqual(result["status"], "read")
        self.assertEqual([job["date_kind"] for job in result["leads"]],
                         ["published", "updated", "created", "unknown"])
        self.assertEqual([job["location_text"] for job in result["leads"]],
                         ["Remote", "London", "Paris", "Paris"])
        self.assertTrue(all(job["status"] == "unverified" for job in result["leads"]))

    def test_coverage_distinguishes_empty_config_from_failure(self):
        profile = {"target_titles": ["Executive Assistant"]}
        self.assertEqual(scan(profile, 7)["status"], "not_configured")
        profile["boards"] = [{"platform": "ashby", "slug": "example"}]

        def unavailable(*args):
            raise URLError("offline")

        result = scan(profile, 7, fetch=unavailable)
        self.assertEqual(result["status"], "partial_or_unavailable")
        self.assertEqual(result["coverage"][0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
