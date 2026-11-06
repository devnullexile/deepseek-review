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

    def __init__(self, head_sha, reviews, files=(), members=()):
        self.head_sha = head_sha
        self.reviews = reviews
        self.files = list(files)
        self.members = None if members is None else set(members)
        self.comments = []
        self.labels = {"review"}
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
                if self.path == "/orgs/o/members/":
                    return self._send(200, [{"login": "alice"}])  # the list endpoint
                if self.path.startswith("/orgs/o/members/"):
                    login = self.path.rsplit("/", 1)[1]
                    if fake.members is None:
                        return self._send(403, {"message": "Resource not accessible by integration"})
                    if login in fake.members:
                        self.send_response(204)
                        self.end_headers()
                        return
                    return self._send(404, {"message": "Not Found"})
                if self.path.startswith("/repos/o/r/pulls/7/files"):
                    return self._send(200, fake.files)
                if self.path == "/repos/o/r/pulls/7":
                    return self._send(200, {"head": {"sha": fake.head_sha},
                                            "changed_files": len(fake.files)})
                if self.path.startswith("/repos/o/r/issues/7/comments"):
                    return self._send(200, fake.comments)
                if self.path.startswith("/repos/o/r/pulls/7/reviews"):
                    page = 2 if "page=2" in self.path else 1
                    half = len(fake.reviews) // 2
                    if page == 1:
                        link = f'<http://{self.headers["Host"]}/repos/o/r/pulls/7/reviews?per_page=100&page=2>; rel="next"'
                        return self._send(200, fake.reviews[:half], [("Link", link)])
                    return self._send(200, fake.reviews[half:])
                self._send(404, {"message": "Not Found"})

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, body, None))
                if self.path == "/repos/o/r/issues/7/comments":
                    fake.comments.append({"id": len(fake.comments) + 1, "user": {"login": BOT},
                                          "body": body["body"]})
                self._send(200, {})

            def do_PATCH(self):
                body = self._body()
                fake.requests.append(("PATCH", self.path, body, None))
                comment_id = int(self.path.rsplit("/", 1)[1])
                for comment in fake.comments:
                    if comment["id"] == comment_id:
                        comment["body"] = body["body"]
                self._send(200, {})

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path, None, None))
                name = self.path.rsplit("/", 1)[1]
                if name in fake.labels:
                    fake.labels.discard(name)
                    return self._send(200, [])
                self._send(404, {"message": "Label does not exist"})

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
        # The result comment has its own tests; the others check reviews only.
        return {"GITHUB_API_URL": fake.url, "GITHUB_REPOSITORY": "o/r", "PR_NUMBER": "7",
                "GH_TOKEN": "secret-token", "RESULT_COMMENT": "false", **extra}

    def start(self, head_sha=HEAD, reviews=(), files=(), members=()):
        fake = FakeGitHub(head_sha, list(reviews), files, members)
        self.addCleanup(fake.close)
        return fake

    def write_review(self, payload):
        path = self.tmp / "review.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def members_only_gate(self, fake, login, owner_type="Organization", draft=False, **env):
        event = self.tmp / "event.json"
        event.write_text(json.dumps({
            "repository": {"owner": {"login": "o", "type": owner_type}},
            "pull_request": {"number": 7, "html_url": "u", "draft": draft,
                             "user": {"login": login} if login is not None else None,
                             "author_association": "CONTRIBUTOR"}}))
        return self.run_cli("gate", **{**self.api_env(
            fake, GITHUB_EVENT_PATH=str(event), ALLOWED_AUTHORS="", MEMBERS_ONLY="true"), **env})

    def test_members_only_gate_skips_a_deleted_author(self):
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"run": "false"})

    def test_members_only_gate_without_a_token_explains_the_permission(self):
        # create-github-app-token fails when the app lacks Members: Read-only; the gate still runs.
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, "alice", GH_TOKEN="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Members", result.stdout + result.stderr)

    def test_members_only_gate_skips_drafts_without_a_token(self):
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, "alice", draft=True, GH_TOKEN="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"run": "false"})

    def test_members_only_gate_asks_github_about_private_membership(self):
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, "alice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["run"], "true")
        self.assertIn(("GET", "/orgs/o/members/alice"), [(m, p) for m, p, _, _ in fake.requests])

    def test_members_only_gate_skips_non_members(self):
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, "mallory")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"run": "false"})
        self.assertIn("mallory", result.stdout)

    def test_members_only_gate_explains_a_missing_app_permission(self):
        fake = self.start(members=None)
        result = self.members_only_gate(fake, "alice")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Members", result.stdout + result.stderr)

    def test_members_only_gate_requires_an_organization(self):
        fake = self.start(members={"alice"})
        result = self.members_only_gate(fake, "alice", owner_type="User")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("organization", result.stdout + result.stderr)

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

    def test_unset_excluded_paths_still_protect_github(self):
        fake = self.start(files=[{"filename": ".github/workflows/x.yml"}])
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(CLEAN)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "not-approved"})

    def test_explicitly_empty_excluded_paths_allow_any_path(self):
        fake = self.start(files=[{"filename": ".github/workflows/x.yml"}])
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(CLEAN),
            APPROVAL_EXCLUDED_PATHS=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "approved"})

    def test_request_changes_flag_posts_a_change_request(self):
        fake = self.start(files=[{"filename": "src/app.py"}])
        blocked = {**CLEAN, "review": {**CLEAN["review"], "merge_recommendation": "changes_required"}}
        result = self.run_cli("approve", **self.api_env(
            fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(blocked),
            APPROVE="true", REQUEST_CHANGES="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"result": "changes-requested"})
        [(method, path, body)] = fake.writes()
        self.assertEqual((method, path), ("POST", "/repos/o/r/pulls/7/reviews"))
        self.assertEqual((body["event"], body["commit_id"]), ("REQUEST_CHANGES", HEAD))
        self.assertTrue(body["body"])

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



class NewCommandTests(ApiCommandTests):
    def test_data_files_merges_the_users_globs_with_this_pull_requests_data(self):
        fake = self.start(files=[
            {"filename": "uv.lock", "additions": 3, "changes": 3, "patch": "@@"},
            {"filename": "src/app.py", "additions": 4000, "changes": 4000, "patch": "@@"},
            {"filename": ".github/workflows/x.yml", "additions": 5000, "changes": 5000}])
        result = self.run_cli("data-files", **self.api_env(
            fake, IGNORE_GLOBS='["*.meta"]', RUNNER_TEMP=str(self.tmp)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.outputs()["ignore-globs"]), ["*.meta", "uv.lock"])
        listed = json.loads((self.tmp / "deepseek-review" / "data-files.json").read_text())
        self.assertEqual(listed, ["uv.lock"])

    def test_data_files_can_be_turned_off(self):
        fake = self.start(files=[{"filename": "uv.lock", "additions": 3, "changes": 3, "patch": "@@"}])
        result = self.run_cli("data-files", **self.api_env(
            fake, SKIP_DATA_FILES="false", RUNNER_TEMP=str(self.tmp)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["ignore-globs"], "")

    def test_the_review_label_is_taken_and_a_missing_one_is_fine(self):
        fake = self.start()
        for _ in range(2):
            result = self.run_cli("take-label", **self.api_env(fake, REVIEW_LABEL="review"))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fake.labels, set())

    def test_new_commits_in_label_mode_withdraw_the_bots_approval_and_say_why(self):
        fake = self.start(reviews=[review_record(1, BOT, "APPROVED", OLD),
                                   review_record(2, "alice", "APPROVED", OLD)])
        result = self.run_cli("stale", **self.api_env(fake, BOT_LOGIN=BOT, RESULT_COMMENT="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        dismissed = [path for method, path, _ in fake.writes() if method == "PUT"]
        self.assertEqual(dismissed, ["/repos/o/r/pulls/7/reviews/1/dismissals"])
        self.assertIn("Add the `review` label", fake.comments[0]["body"])

    def test_approve_keeps_one_result_comment_up_to_date(self):
        fake = self.start()
        review = self.write_review(CLEAN)
        env = self.api_env(fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=review,
                           APPROVAL_EXCLUDED_PATHS="", RESULT_COMMENT="true", RUNNER_TEMP=str(self.tmp))
        for _ in range(2):
            result = self.run_cli("approve", **env)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(fake.comments), 1)
        self.assertIn("Approved", fake.comments[0]["body"])

    def test_gate_skips_other_labels_and_reports_stale_commits_in_label_mode(self):
        event = self.tmp / "event.json"
        pr = {"number": 7, "html_url": "u", "draft": False, "user": {"login": "alice"},
              "author_association": "OWNER"}
        cases = [({"action": "labeled", "label": {"name": "bug"}}, "push", {"run": "false"}),
                 ({"action": "synchronize"}, "label", {"run": "false", "stale": "true", "pr-number": "7"}),
                 ({"action": "labeled", "label": {"name": "review"}}, "label",
                  {"run": "true", "pr-number": "7", "pr-url": "u", "label-requested": "true"})]
        for payload, trigger, expected in cases:
            with self.subTest(payload=payload, trigger=trigger):
                self.output.write_text("")
                event.write_text(json.dumps(dict(payload, pull_request=pr)))
                result = self.run_cli("gate", GITHUB_EVENT_PATH=str(event), ALLOWED_AUTHORS="",
                                      REVIEW_ON=trigger)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.outputs(), expected)

    def test_gate_refuses_an_unknown_review_mode(self):
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"action": "opened", "pull_request": {"number": 7}}))
        result = self.run_cli("gate", GITHUB_EVENT_PATH=str(event), REVIEW_ON="sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review-on", result.stdout + result.stderr)


class LabelModeGateTests(CliTestCase):
    def test_a_push_to_a_pull_request_the_bot_skips_never_touches_it(self):
        event = self.tmp / "event.json"
        for pr in ({"number": 7, "html_url": "u", "draft": True, "user": {"login": "alice"},
                    "author_association": "OWNER"},
                   {"number": 7, "html_url": "u", "draft": False, "user": {"login": "stranger"},
                    "author_association": "NONE"}):
            with self.subTest(pr=pr):
                self.output.write_text("")
                event.write_text(json.dumps({"action": "synchronize", "pull_request": pr}))
                result = self.run_cli("gate", GITHUB_EVENT_PATH=str(event), ALLOWED_AUTHORS="",
                                      REVIEW_ON="label")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.outputs(), {"run": "false"})


class StaleAndValidationTests(ApiCommandTests):
    def test_a_stale_run_keeps_an_approval_of_the_current_head(self):
        fake = self.start(head_sha=HEAD, reviews=[review_record(1, BOT, "APPROVED", OLD),
                                                  review_record(2, BOT, "APPROVED", HEAD)])
        result = self.run_cli("stale", **self.api_env(fake, BOT_LOGIN=BOT, RESULT_COMMENT="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        # The current head is already approved, so nothing is stale and nothing changes.
        self.assertEqual(fake.writes(), [])

    def test_an_invalid_flag_is_refused_before_any_change(self):
        fake = self.start(reviews=[review_record(1, BOT, "APPROVED", OLD)])
        review = self.write_review(CLEAN)
        for command, env in (("stale", dict(BOT_LOGIN=BOT, RESULT_COMMENT="yes")),
                             ("approve", dict(BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=review,
                                              APPROVAL_EXCLUDED_PATHS="", RESULT_COMMENT="False"))):
            with self.subTest(command=command):
                result = self.run_cli(command, **self.api_env(fake, **env))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("result-comment", result.stdout + result.stderr)
        self.assertEqual(fake.writes(), [])


class LateEventTests(ApiCommandTests):
    def test_a_stale_run_after_a_review_of_the_current_head_changes_nothing(self):
        fake = self.start(head_sha=HEAD, reviews=[review_record(1, BOT, "APPROVED", OLD),
                                                  review_record(2, BOT, "CHANGES_REQUESTED", HEAD)])
        result = self.run_cli("stale", **self.api_env(fake, BOT_LOGIN=BOT, RESULT_COMMENT="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fake.writes(), [])

    def test_data_files_falls_back_to_the_users_globs_when_github_fails(self):
        fake = self.start()
        env = self.api_env(fake, IGNORE_GLOBS='["*.meta"]', RUNNER_TEMP=str(self.tmp))
        env["GITHUB_API_URL"] = "http://127.0.0.1:9"  # Nothing listens: the listing fails.
        result = self.run_cli("data-files", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.outputs()["ignore-globs"]), ["*.meta"])
        self.assertIn("could not list", result.stdout)

if __name__ == "__main__":
    unittest.main()
