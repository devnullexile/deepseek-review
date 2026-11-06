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
    "gate": {"ALLOWED_AUTHORS", "IGNORE_GLOBS", "GH_TOKEN", "MEMBERS_ONLY", "REVIEW_ON", "REVIEW_LABEL"},
    "head-sha": {"GH_TOKEN", "PR_NUMBER"},
    "approve": {"GH_TOKEN", "PR_NUMBER", "BOT_LOGIN", "HEAD_SHA_BEFORE", "REVIEW_JSON",
                "APPROVAL_EXCLUDED_PATHS", "APPROVE", "REQUEST_CHANGES", "RESULT_COMMENT"},
    "data-files": {"GH_TOKEN", "PR_NUMBER", "IGNORE_GLOBS", "SKIP_DATA_FILES", "DATA_FILE_LINES",
                   "APPROVAL_EXCLUDED_PATHS"},
    "take-label": {"GH_TOKEN", "PR_NUMBER", "REVIEW_LABEL"},
    "stale": {"GH_TOKEN", "PR_NUMBER", "BOT_LOGIN", "APPROVE", "RESULT_COMMENT", "REVIEW_LABEL"},
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

    def test_gate_runs_even_when_the_token_step_was_skipped_or_failed(self):
        # Drafts and a missing app permission must still reach the gate's own handling.
        [gate] = [step for step in self.steps if step.get("id") == "gate"]
        self.assertIn("!cancelled()", gate.get("if", ""))
        self.assertNotIn("steps.token", gate.get("if", ""))

    def test_the_token_step_passes_only_client_id_so_no_deprecation_warning_appears(self):
        # create-github-app-token deprecates app-id; the runner warns whenever it appears in `with`,
        # even empty. Its code reads client-id or app-id into the same field, so an App ID works too.
        [token] = [step for step in self.steps if step.get("id") == "token"]
        self.assertNotIn("app-id", token["with"])
        self.assertEqual(token["with"]["client-id"], "${{ inputs.client-id || inputs.app-id }}")

    def test_our_app_id_input_is_deprecated_in_favour_of_client_id(self):
        self.assertIn("client-id", self.manifest["inputs"]["app-id"].get("deprecationMessage", ""))

    def test_the_review_reads_the_ignore_list_with_this_pull_requests_data_files(self):
        [review_step] = [step for step in self.steps if "docker run" in step.get("run", "")]
        self.assertEqual(review_step["env"]["IGNORE_GLOBS"], "${{ steps.data.outputs.ignore-globs }}")
        ids = [step.get("id") for step in self.steps]
        self.assertLess(ids.index("data"), self.steps.index(review_step))

    def test_earlier_findings_get_more_room_than_pr_agents_default(self):
        [review_step] = [step for step in self.steps if "docker run" in step.get("run", "")]
        self.assertEqual(review_step["env"]["PR_REVIEWER__MAX_PREVIOUS_FINDINGS_CHARS"],
                         "${{ inputs.max-previous-findings-chars }}")
        self.assertGreater(int(self.manifest["inputs"]["max-previous-findings-chars"]["default"]), 8000)

    def test_the_result_comment_is_kept_even_when_approval_is_off(self):
        [approval] = [step for step in self.steps if step.get("id") == "approval"]
        self.assertIn("inputs.result-comment == 'true'", approval["if"])

    def test_every_input_reference_is_declared(self):
        declared = set(self.manifest["inputs"])
        used = set(re.findall(r"inputs\.([\w-]+)", MANIFEST.read_text()))
        self.assertEqual(used - declared, set())


if __name__ == "__main__":
    unittest.main()
