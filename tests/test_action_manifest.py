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
    "gate": {"ALLOWED_AUTHORS", "IGNORE_GLOBS"},
    "head-sha": {"GH_TOKEN", "PR_NUMBER"},
    "approve": {"GH_TOKEN", "PR_NUMBER", "BOT_LOGIN", "HEAD_SHA_BEFORE", "REVIEW_JSON"},
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

    def test_every_input_reference_is_declared(self):
        declared = set(self.manifest["inputs"])
        used = set(re.findall(r"inputs\.([\w-]+)", MANIFEST.read_text()))
        self.assertEqual(used - declared, set())


if __name__ == "__main__":
    unittest.main()
