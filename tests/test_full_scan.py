import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

import job_history
import scan_ats
import search_worker
import settings
from pipeline import annotate_job, relevant_role_title


class FullScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.local = self.directory / ".local"
        self.local.mkdir()
        self.candidate = {
            "confirmed": True,
            "target_titles": ["Executive Assistant", "Content Creator", "Marketing Manager"],
            "title_aliases": ["Executive Business Partner"],
            "excluded_titles": ["intern"],
            "search_terms": ["campaign reporting"],
            "selected_lanes": ["custom-market"],
            "markets": {"custom-market": {"locations": ["Toronto"], "countries": ["ca"]}},
            "hard_constraints": ["sensitive local note"],
            "evidence": ["sensitive local evidence"],
        }
        (self.local / "profile.json").write_text(json.dumps(self.candidate), encoding="utf-8")
        self.addCleanup(settings.profile.cache_clear)
        self.patch = patch.object(settings, "LOCAL", self.local)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        settings.profile.cache_clear()

    def test_portable_role_filters_cover_three_families(self):
        config = settings.config_data("search-config.json", require_profile=True)
        for title in self.candidate["target_titles"] + self.candidate["title_aliases"]:
            with self.subTest(title=title):
                self.assertTrue(relevant_role_title("Senior " + title, config))
                self.assertFalse(relevant_role_title(title + " Intern", config))
                row = annotate_job({"title": title, "location": "Toronto", "company": "Sample",
                                    "url": "https://example.org/careers/target"}, config)
                self.assertEqual(row["fit_signal"], "medium")
                self.assertTrue(scan_ats.lane_matches(row, "custom-market", {}))
        self.assertFalse(relevant_role_title("Software Developer", config))
        radar = settings.config_data("job-radar.json")
        self.assertEqual(radar["titles"], self.candidate["target_titles"] + self.candidate["title_aliases"])
        self.assertFalse(relevant_role_title("Content Creator Intern", radar))

    def test_cli_generates_custom_market_full_scan_without_network(self):
        for dirname in ("automation", "config", "third-party"):
            shutil.copytree(ROOT / dirname, self.directory / dirname,
                            ignore=shutil.ignore_patterns("__pycache__"))
        result = subprocess.run([sys.executable, "automation/run_rotation.py", "--broad", "--days", "7"],
                                cwd=self.directory, timeout=25, capture_output=True, text=True,
                                encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_dir, = (self.local / "state" / "runs").glob("*-agent-assignments")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        lane = manifest["lanes"]["custom-market"]
        self.assertEqual(lane["ats_search_generation"]["returncode"], 0)
        self.assertFalse(lane["execution"])
        manual_raw = (self.directory / lane["manual_queries"]).read_text(encoding="utf-8")
        jobs = {t["source"]: t for t in json.loads(manual_raw)["tasks"]}
        self.assertEqual(jobs["LinkedIn Jobs"]["native_status"], "pending")
        self.assertIn("Toronto", jobs["LinkedIn Jobs"]["native_urls"][0])
        self.assertTrue(any("linkedin.com/posts/" in query for query in jobs["LinkedIn Hiring Posts"]["indexed_queries"]))
        self.assertTrue(any("linkedin.com/feed/update/" in query for query in jobs["LinkedIn Hiring Posts"]["indexed_queries"]))
        self.assertIn("Indeed country sites", jobs)
        self.assertNotIn("sensitive local", manual_raw)
        self.assertIn("marketing", (self.directory / lane["ats_search_queries"]).read_text(encoding="utf-8").lower())

    def test_repeat_is_not_counted_as_new(self):
        state = {"version": 1, "seen": {}}
        config = settings.config_data("search-config.json")
        def candidate():
            return annotate_job({"title": "Marketing Manager", "company": "Sample",
                                 "url": "https://example.org/careers/7?utm_source=example"}, config)
        self.assertEqual(job_history.reconcile_jobs(state, [candidate()], "first",
                                                    lambda job: False, "2026-01-01")["new"], 1)
        self.assertEqual(job_history.reconcile_jobs(state, [candidate()], "mirror",
                                                    lambda job: False, "2026-01-02")["new"], 0)

    def test_external_worker_uses_explicit_model_and_empty_workspace(self):
        def runner(args, cwd, **kwargs):
            self.assertEqual(args[1:4], ["run", "--model", "provider/free"])
            self.assertNotEqual(Path(cwd), ROOT)
            self.assertEqual(list(Path(cwd).iterdir()), [])
            self.assertIn("Content Creator", args[-1])
            self.assertNotIn("sensitive local", args[-1])
            return subprocess.CompletedProcess(args, 0, "unverified", "")

        with patch.object(search_worker.shutil, "which", return_value="/test/kilo"):
            report = search_worker.run_worker("kilo", "provider/free", ["Content Creator Toronto"], 10, runner)
        self.assertEqual(report["status"], "worker_output_unverified")


if __name__ == "__main__":
    unittest.main()
