import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path
from unittest.mock import patch

import job_hunter as legacy
import job_hunter_robust as runner
import monitor_job_hunter as monitor


class UpdateTests(unittest.TestCase):
    def test_october_cutoff_is_inclusive(self):
        config = runner.load_config(None)
        self.assertEqual(config["posted_after"], "2026-10-01")
        for day, expected in (("2026-09-30", True), ("2026-10-01", False),
                              ("2026-10-02", False)):
            job = legacy.Job(title="Robotics Intern", company="NVIDIA", posted_date=day)
            self.assertEqual(bool(runner.posted_date_filter_reason(job, config)), expected)

    def test_month_day_parse_has_no_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            parsed = runner.parse_posted_date("Oct 13", today=date(2026, 10, 28))
        self.assertEqual(parsed, date(2026, 10, 13))
        self.assertFalse(any(item.category is DeprecationWarning for item in seen))

    def test_keyword_fallback_is_labeled(self):
        job = legacy.Job(title="Robotics Intern", description="robotics controls")
        _, reason = legacy.ai_score(job, {})
        self.assertIn("Fallback keyword score", reason)

    def test_negated_citizenship_requirement_is_not_disqualifying(self):
        config = runner.load_config(None)
        job = legacy.Job(title="Robotics Intern", description="U.S. citizenship is not required for this role.")
        self.assertEqual(runner.disqualification_reason(job, config), "")
        job.description = "U.S. citizenship is required for this role."
        self.assertIn("citizenship", runner.disqualification_reason(job, config))

    def test_failed_documents_remain_retryable_without_resending_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(runner, "STATE_DIR", root), patch.object(runner, "REPORT_DIR", root):
                state = runner.StateStore(root / "state.sqlite")
                try:
                    job = legacy.Job(title="Robotics Intern", company="Example", url="https://example.com/job")
                    uid, _, _ = state.upsert_discovered(job)
                    state.mark_notified(uid, 80, "match", ">=72% match")
                    state.mark_documents(uid, False, "temporary error")
                    self.assertEqual(state.get_by_uid(uid)["status"], "notified")
                    self.assertEqual(len(state.failed_documents()), 1)
                    state.mark_documents(uid, True)
                    self.assertEqual(len(state.failed_documents()), 0)
                    self.assertEqual(state.get_by_uid(uid)["status"], "notified")
                    self.assertEqual(runner.export_jobs_csv(state).name, "jobs.csv")
                finally:
                    state.close()

    def test_monitor_alerts_once_per_stale_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "last_run_summary.json"
            report.write_text('{"finished_at":"2026-10-13T20:37:22"}', encoding="utf-8")
            response = unittest.mock.Mock()
            response.raise_for_status.return_value = None
            with patch.object(monitor, "REPORT", report), patch.object(monitor, "MARKER", root / "marker.json"), \
                 patch.object(monitor, "ROOT", root), patch.dict("os.environ", {"DISCORD_WEBHOOK": "https://example.test/hook"}), \
                 patch.object(monitor.requests, "post", return_value=response) as post:
                monitor.main()
                monitor.main()
                post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
