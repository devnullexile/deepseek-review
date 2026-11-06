"""Checks for this repository's review workflow and the workflow examples in the READMEs."""
import os
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # CI installs PyYAML and sets REQUIRE_PYYAML; see .github/workflows/test.yml.
    if os.environ.get("REQUIRE_PYYAML"):
        raise
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = re.compile(r"```yaml\n(name: DeepSeek PR Review\n.*?)```", re.DOTALL)
RETARGET_ONLY = "github.event.action != 'edited' || github.event.changes.base"
EDIT_GROUP = "${{ github.event.action == 'edited' && !github.event.changes.base && '-edit' || '' }}"


def _workflows():
    yield ".github/workflows/review.yml", (ROOT / ".github/workflows/review.yml").read_text()
    for readme in ("README.md", "README.ru.md"):
        examples = EXAMPLE.findall((ROOT / readme).read_text())
        assert len(examples) == 1, f"{readme}: expected one workflow example, found {len(examples)}"
        yield readme, examples[0]


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class ReviewWorkflowTests(unittest.TestCase):
    def test_a_retargeted_pull_request_is_reviewed_again(self):
        # Retargeting is an edited event that changes the diff without a new commit, so an
        # approval of the old diff would otherwise stay on the same commit.
        for name, text in _workflows():
            with self.subTest(workflow=name):
                workflow = yaml.safe_load(text)
                triggers = workflow.get("on", workflow.get(True))
                self.assertIn("edited", triggers["pull_request_target"]["types"])
                self.assertIn(RETARGET_ONLY, workflow["jobs"]["review"]["if"])

    def test_a_title_or_body_edit_never_cancels_a_running_review(self):
        for name, text in _workflows():
            with self.subTest(workflow=name):
                group = yaml.safe_load(text)["concurrency"]["group"]
                self.assertEqual(group, "deepseek-review-${{ github.event.pull_request.number }}"
                                 + EDIT_GROUP)


if __name__ == "__main__":
    unittest.main()
