"""Unit test wrapper executing the Golden Dataset Regression Evaluation Suite."""

from __future__ import annotations

import unittest

from eval.run_eval import run_golden_evaluation


class TestGoldenEvaluationSuite(unittest.TestCase):
    def test_all_golden_benchmark_cases_pass(self) -> None:
        report = run_golden_evaluation()
        self.assertTrue(
            report["all_passed"],
            f"Golden evaluation regression detected: {report}",
        )
        self.assertEqual(report["pass_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
