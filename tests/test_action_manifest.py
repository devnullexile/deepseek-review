"""Contract checks for action.yml; actionlint does not lint composite action manifests."""
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

MANIFEST = Path(os.environ.get("ACTION_MANIFEST",
                               Path(__file__).resolve().parents[1] / "action.yml"))
STEP_KEYS = {"name", "id", "if", "shell", "env", "run", "uses", "with"}
COMMAND_ENV = {
    "gate": {"ALLOWED_AUTHORS", "IGNORE_GLOBS", "GH_TOKEN", "MEMBERS_ONLY"},
    "head-sha": {"GH_TOKEN", "PR_NUMBER"},
    "approve": {"GH_TOKEN", "PR_NUMBER", "BOT_LOGIN", "HEAD_SHA_BEFORE", "REVIEW_JSON",
                "APPROVAL_EXCLUDED_PATHS", "APPROVE", "REQUEST_CHANGES"},
}


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class ActionManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = yaml.safe_load(MANIFEST.read_text())
        self.steps = self.manifest["runs"]["steps"]

    def test_steps_use_only_known_keys(self):
        for step in self.steps:
            with self.subTest(step=step.get("name")):
                self.assertEqual(set(step) - STEP_KEYS, set())

    def test_each_script_command_receives_its_environment(self):
        seen = set()
        for step in self.steps:
            match = re.search(r"review_gate\.py\"?\s+([\w-]+)", step.get("run", ""))
            if not match:
                continue
            command = match.group(1)
            seen.add(command)
            with self.subTest(command=command):
                self.assertLessEqual(COMMAND_ENV[command], set(step.get("env") or {}))
        self.assertEqual(seen, set(COMMAND_ENV))

    def test_default_token_window_leaves_input_capacity(self):
        # pr-agent reserves max-output-tokens inside max-model-tokens; the rest is prompt input.
        inputs = self.manifest["inputs"]
        window = int(inputs["max-model-tokens"]["default"])
        output = int(inputs["max-output-tokens"]["default"])
        self.assertGreaterEqual(window - output, 100_000)

    def test_pr_agent_knows_the_bot_login_it_posts_as(self):
        # An installation token cannot call GET /user; without this, pr-agent assumes it is
        # github-actions[bot], fails to recognize its own comments, and posts a new summary
        # comment on every run instead of updating one.
        [review_step] = [step for step in self.steps if "docker run" in step.get("run", "")]
        login = review_step["env"].get("GITHUB__AGENT_LOGIN", "")
        self.assertEqual(login, "${{ steps.token.outputs.app-slug }}[bot]")

    def test_token_is_created_before_the_gate_that_uses_it(self):
        ids = [step.get("id") for step in self.steps]
        self.assertLess(ids.index("token"), ids.index("gate"))

    def test_every_input_reference_is_declared(self):
        declared = set(self.manifest["inputs"])
        used = set(re.findall(r"inputs\.([\w-]+)", MANIFEST.read_text()))
        self.assertEqual(used - declared, set())


if __name__ == "__main__":
    unittest.main()
