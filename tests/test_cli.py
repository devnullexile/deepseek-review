import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_gate.py"
BOT = "devnullexile-deepseek-review[bot]"
HEAD = "c" * 40
OLD = "d" * 40
CLEAN = {
    "review": {"merge_recommendation": "no_concerns_found", "key_issues_to_review": [],
               "security_concerns": "No"},
    "coverage": {"files_not_reviewed": [], "files_partially_reviewed": []},
}


class FakeGitHub:
    """Minimal in-memory GitHub REST API for one pull request."""

    def __init__(self, head_sha, reviews, files=()):
        self.head_sha = head_sha
        self.reviews = reviews
        self.files = list(files)
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=()):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                fake.requests.append(("GET", self.path, None, self.headers.get("Authorization")))
                if self.path.startswith("/repos/o/r/pulls/7/files"):
                    return self._send(200, fake.files)
                if self.path == "/repos/o/r/pulls/7":
                    return self._send(200, {"head": {"sha": fake.head_sha},
                                            "changed_files": len(fake.files)})
                if self.path.startswith("/repos/o/r/pulls/7/reviews"):
                    page = 2 if "page=2" in self.path else 1
                    half = len(fake.reviews) // 2
                    if page == 1:
                        link = f'<http://{self.headers["Host"]}/repos/o/r/pulls/7/reviews?per_page=100&page=2>; rel="next"'
                        return self._send(200, fake.reviews[:half], [("Link", link)])
                    return self._send(200, fake.reviews[half:])
                self._send(404, {"message": "Not Found"})

            def do_POST(self):
                fake.requests.append(("POST", self.path, self._body(), None))
                self._send(200, {})

            def do_PUT(self):
                fake.requests.append(("PUT", self.path, self._body(), None))
                self._send(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def writes(self):
        return [(method, path, body) for method, path, body, _ in self.requests if method != "GET"]


def review_record(review_id, login, state, commit_id):
    return {"id": review_id, "user": {"login": login}, "state": state, "commit_id": commit_id}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "github_output"
        self.output.write_text("")

    def run_cli(self, *args, **env):
        full_env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(self.output), **env}
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=full_env,
                              capture_output=True, text=True, timeout=30)

    def outputs(self):
        pairs = (line.split("=", 1) for line in self.output.read_text().splitlines() if line)
        return dict(pairs)


class GateCommandTests(CliTestCase):
    def write_event(self, event):
        path = self.tmp / "event.json"
        path.write_text(json.dumps(event))
        return str(path)

    def test_allowed_author_produces_run_outputs(self):
        event = self.write_event({"pull_request": {
            "number": 7, "html_url": "https://github.com/o/r/pull/7", "draft": False,
            "user": {"login": "alice"}, "author_association": "COLLABORATOR"}})
        result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(),
                         {"run": "true", "pr-number": "7", "pr-url": "https://github.com/o/r/pull/7"})

    def test_allowed_authors_accepts_commas_and_newlines(self):
        event = self.write_event({"pull_request": {
            "number": 7, "html_url": "u", "draft": False,
            "user": {"login": "carol"}, "author_association": "NONE"}})
        result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="alice, bob\ncarol\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["run"], "true")

    def test_refused_author_skips_with_a_visible_reason(self):
        event = self.write_event({"pull_request": {
            "number": 7, "html_url": "u", "draft": False,
            "user": {"login": "stranger"}, "author_association": "NONE"}})
        result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"run": "false"})
        self.assertIn("stranger", result.stdout)

    def test_valid_ignore_globs_are_accepted(self):
        event = self.write_event({"pull_request": {
            "number": 7, "html_url": "u", "draft": False,
            "user": {"login": "alice"}, "author_association": "OWNER"}})
        for globs in ("", '["*.meta", "docs/*/evidence/*"]', "[]"):
            with self.subTest(globs=globs):
                result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="",
                                      IGNORE_GLOBS=globs)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_ignore_globs_fail_before_any_review(self):
        event = self.write_event({"pull_request": {
            "number": 7, "html_url": "u", "draft": False,
            "user": {"login": "alice"}, "author_association": "OWNER"}})
        for globs in ("*.meta", '{"glob": "*.meta"}', '["*.meta", 3]', '["*.meta",'):
            with self.subTest(globs=globs):
                self.output.write_text("")
                result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="",
                                      IGNORE_GLOBS=globs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ignore-globs", result.stdout + result.stderr)
                self.assertEqual(self.outputs(), {})

    def test_non_pull_request_event_fails(self):
        event = self.write_event({"push": {}})
        result = self.run_cli("gate", GITHUB_EVENT_PATH=event, ALLOWED_AUTHORS="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pull_request", result.stdout + result.stderr)


class ApiCommandTests(CliTestCase):
    def api_env(self, fake, **extra):
        return {"GITHUB_API_URL": fake.url, "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "7",
                "GH_TOKEN": "secret-token", **extra}

    def start(self, head_sha=HEAD, reviews=(), files=()):
        fake = FakeGitHub(head_sha, list(reviews), files)
        self.addCleanup(fake.close)
        return fake

    def write_review(self, payload):
        path = self.tmp / "review.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def test_head_sha_command_records_the_current_head(self):
        fake = self.start()
        result = self.run_cli("head-sha", **self.api_env(fake))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"sha": HEAD})
        self.assertEqual(fake.requests[0][3], "Bearer secret-token")

    def test_approve_posts_an_approval_pinned_to_the_reviewed_commit(self):
        fake = self.start(reviews=[review_record(1, "alice", "COMMENTED", OLD),
                                   review_record(2, BOT, "COMMENTED", OLD)])
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(CLEAN)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "approved"})
        [(method, path, body)] = fake.writes()
        self.assertEqual((method, path), ("POST", "/repos/o/r/pulls/7/reviews"))
        self.assertEqual((body["event"], body["commit_id"]), ("APPROVE", HEAD))

    def test_approve_dismisses_bot_approvals_found_on_any_page(self):
        fake = self.start(reviews=[review_record(1, "alice", "APPROVED", OLD),
                                   review_record(2, BOT, "APPROVED", OLD)])
        blocked = {**CLEAN, "review": {**CLEAN["review"], "merge_recommendation": "changes_required"}}
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(blocked)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "not-approved"})
        [(method, path, body)] = fake.writes()
        self.assertEqual((method, path), ("PUT", "/repos/o/r/pulls/7/reviews/2/dismissals"))
        self.assertEqual(body["event"], "DISMISS")
        self.assertIn("changes_required", body["message"])
        self.assertIn("changes_required", result.stdout)

    def test_excluded_paths_from_the_environment_prevent_approval(self):
        fake = self.start(files=[{"filename": "src/app.py"}, {"filename": ".github/workflows/x.yml"}])
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(CLEAN),
            APPROVAL_EXCLUDED_PATHS=".github/*\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "not-approved"})
        self.assertEqual(fake.writes(), [])
        self.assertIn(".github/workflows/x.yml", result.stdout)

    def test_missing_review_file_never_approves(self):
        fake = self.start()
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=str(self.tmp / "absent.json")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "not-approved"})
        self.assertEqual(fake.writes(), [])

    def test_head_moved_since_review_started_changes_nothing(self):
        fake = self.start(head_sha=HEAD, reviews=[review_record(2, BOT, "APPROVED", OLD)])
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=OLD, REVIEW_JSON=self.write_review(CLEAN)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "head-changed"})
        self.assertEqual(fake.writes(), [])

    def test_api_error_fails_the_step(self):
        fake = self.start()
        result = self.run_cli("head-sha", **self.api_env(fake, PR_NUMBER="404"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("404", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
