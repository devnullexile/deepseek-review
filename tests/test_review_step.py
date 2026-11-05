"""Runs the review step's own shell script from action.yml against a fake docker."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # CI installs PyYAML and sets REQUIRE_PYYAML; see .github/workflows/test.yml.
    if os.environ.get("REQUIRE_PYYAML"):
        raise
    yaml = None

MANIFEST = Path(__file__).resolve().parents[1] / "action.yml"
GOOD = {"review": {"merge_recommendation": "no_concerns_found"}}

# Each call takes the next behavior from the plan: ok writes a review, fail exits 1,
# nojson exits 0 without output, badjson exits 0 with unreadable output.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
calls = os.environ["FAKE_CALLS"]
with open(calls, "a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
count = sum(1 for _ in open(calls))
plan = os.environ["FAKE_PLAN"].split(",")
behavior = plan[min(count, len(plan)) - 1]
args = sys.argv[1:]
out = args[args.index("-v") + 1].split(":")[0]
target = os.path.join(out, "review.json")
if behavior == "ok":
    json.dump(json.loads(os.environ["FAKE_REVIEW"]), open(target, "w"))
elif behavior == "badjson":
    open(target, "w").write("{not json")
sys.exit(1 if behavior == "fail" else 0)
'''


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class ReviewStepTests(unittest.TestCase):
    def setUp(self):
        manifest = yaml.safe_load(MANIFEST.read_text())
        self.inputs = manifest["inputs"]
        [self.step] = [step for step in manifest["runs"]["steps"] if "docker run" in step.get("run", "")]
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.dir = Path(self.scratch.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.bin = bin_dir
        self.calls = self.dir / "calls.txt"

    def run_step(self, plan, attempts="2"):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.dir),
            "PR_URL": "https://github.com/o/r/pull/1",
            "PR_AGENT_IMAGE": "pragent/pr-agent:test",
            "BASE_INSTRUCTIONS": "Base.\n",
            "EXTRA_INSTRUCTIONS": "",
            "IGNORE_GLOBS": "",
            "REVIEW_ATTEMPTS": attempts,
            "RETRY_DELAY_SECONDS": "0",
            "FAKE_CALLS": str(self.calls),
            "FAKE_PLAN": ",".join(plan),
            "FAKE_REVIEW": json.dumps(GOOD),
        }
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.step["run"]],
                                capture_output=True, text=True, env=env, timeout=60)
        self.call_count = len(self.calls.read_text().splitlines()) if self.calls.exists() else 0
        return result

    def review(self):
        return json.loads((self.dir / "deepseek-review" / "review.json").read_text())

    def test_a_first_successful_attempt_runs_once(self):
        result = self.run_step(["ok"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call_count, 1)
        self.assertEqual(self.review(), GOOD)

    def test_a_failed_attempt_is_retried(self):
        result = self.run_step(["fail", "ok"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.call_count, 2)
        self.assertEqual(self.review(), GOOD)

    def test_an_attempt_without_review_output_is_retried(self):
        result = self.run_step(["nojson", "ok"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.call_count, 2)

    def test_an_attempt_with_unreadable_review_output_is_retried(self):
        result = self.run_step(["badjson", "ok"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.call_count, 2)
        self.assertEqual(self.review(), GOOD)

    def test_the_step_fails_after_its_last_attempt(self):
        result = self.run_step(["fail", "fail", "fail"], attempts="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.call_count, 2)
        self.assertFalse((self.dir / "deepseek-review" / "review.json").exists())

    def test_one_attempt_means_no_retry(self):
        result = self.run_step(["fail", "ok"], attempts="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.call_count, 1)

    def test_an_invalid_attempt_count_is_refused_before_any_review(self):
        for attempts in ("0", "two", ""):
            with self.subTest(attempts=attempts):
                self.calls.unlink(missing_ok=True)
                result = self.run_step(["ok"], attempts=attempts)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.call_count, 0)

    def test_defaults_retry_once(self):
        self.assertEqual(self.inputs["review-attempts"]["default"], "2")
        self.assertIn("retry-delay-seconds", self.inputs)


if __name__ == "__main__":
    unittest.main()
