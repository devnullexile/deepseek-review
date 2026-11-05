import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_gate  # noqa: E402

BOT = "devnullexile-deepseek-review[bot]"
HEAD = "a" * 40
OLD = "b" * 40


def clean_review(**review_overrides):
    review = {
        "merge_recommendation": "no_concerns_found",
        "key_issues_to_review": [],
        "security_concerns": "No",
    }
    review.update(review_overrides)
    return {
        "review": review,
        "coverage": {"files_not_reviewed": [], "files_partially_reviewed": []},
        "usage": {},
    }


def pull_request(login="alice", association="OWNER", draft=False):
    return {"user": {"login": login}, "author_association": association, "draft": draft}


class FakeClient:
    def __init__(self, reviews=(), files=(), changed_files=None):
        self.reviews = list(reviews)
        self.files = list(files)
        self.changed_files = len(self.files) if changed_files is None else changed_files
        self.file_requests = 0
        self.approved = []
        self.requested = []
        self.dismissed = []

    def list_reviews(self):
        return list(self.reviews)

    def list_files(self):
        self.file_requests += 1
        return list(self.files)

    def changed_file_count(self):
        return self.changed_files

    def approve(self, commit_sha, body):
        self.approved.append((commit_sha, body))

    def request_changes(self, commit_sha, body):
        self.requested.append((commit_sha, body))

    def dismiss(self, review_id, message):
        self.dismissed.append((review_id, message))


def review_record(review_id, login, state, commit_id):
    return {"id": review_id, "user": {"login": login}, "state": state, "commit_id": commit_id}


class AuthorAllowedTests(unittest.TestCase):
    def test_repository_writers_are_allowed_without_an_explicit_list(self):
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                allowed, _ = review_gate.author_allowed(pull_request(association=association), [])
                self.assertTrue(allowed)

    def test_outside_contributors_are_refused_without_an_explicit_list(self):
        for association in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"):
            with self.subTest(association=association):
                allowed, reason = review_gate.author_allowed(pull_request(association=association), [])
                self.assertFalse(allowed)
                self.assertIn(association, reason)

    def test_explicit_list_replaces_the_association_rule_and_ignores_case(self):
        allowed, _ = review_gate.author_allowed(
            pull_request(login="Bob", association="NONE"), ["alice", "bob"])
        self.assertTrue(allowed)
        allowed, reason = review_gate.author_allowed(
            pull_request(login="mallory", association="OWNER"), ["alice"])
        self.assertFalse(allowed)
        self.assertIn("mallory", reason)

    def test_draft_pull_requests_are_skipped(self):
        allowed, reason = review_gate.author_allowed(pull_request(draft=True), [])
        self.assertFalse(allowed)
        self.assertIn("draft", reason)


class ApprovalBlockerTests(unittest.TestCase):
    def test_clean_complete_review_has_no_blockers(self):
        self.assertEqual(review_gate.approval_blockers(clean_review()), [])

    def test_missing_or_malformed_output_blocks_approval(self):
        for review in (None, {}, {"review": "text"}, []):
            with self.subTest(review=review):
                self.assertTrue(review_gate.approval_blockers(review))

    def test_any_recommendation_other_than_no_concerns_blocks(self):
        for value in ("needs_review", "changes_required", None):
            with self.subTest(value=value):
                blockers = review_gate.approval_blockers(clean_review(merge_recommendation=value))
                self.assertTrue(blockers)

    def test_reported_key_issues_block(self):
        review = clean_review(key_issues_to_review=[{"relevant_file": "a.py", "issue_header": "Bug"}])
        self.assertTrue(review_gate.approval_blockers(review))

    def test_security_concerns_block_but_negative_answers_do_not(self):
        # Mirrors pr-agent's is_value_no(); YAML parses a bare `No` answer as False.
        for value in ("No", "no\n", "None", "false", False, "", None):
            with self.subTest(value=value):
                self.assertEqual(review_gate.approval_blockers(clean_review(security_concerns=value)), [])
        # pr-agent renders these as "Security concerns", so the bot must not approve them.
        for value in ("No security concerns identified", "Not escaped: SQL injection in query()",
                      "Token is logged", True):
            with self.subTest(value=value):
                self.assertTrue(review_gate.approval_blockers(clean_review(security_concerns=value)))

    def test_incomplete_coverage_blocks(self):
        for key in ("files_not_reviewed", "files_partially_reviewed"):
            with self.subTest(key=key):
                review = clean_review()
                review["coverage"][key] = ["big.unity"]
                self.assertTrue(review_gate.approval_blockers(review))
        review = clean_review()
        del review["coverage"]
        self.assertTrue(review_gate.approval_blockers(review))


class PathBlockerTests(unittest.TestCase):
    def test_matching_files_block_and_are_named(self):
        files = [{"filename": "src/app.py"}, {"filename": ".github/workflows/ci.yml"}]
        blockers = review_gate.path_blockers(files, [".github/*"])
        self.assertEqual(len(blockers), 1)
        self.assertIn(".github/workflows/ci.yml", blockers[0])

    def test_renames_out_of_a_protected_path_also_block(self):
        files = [{"filename": "old-ci.yml", "previous_filename": ".github/workflows/ci.yml"}]
        self.assertTrue(review_gate.path_blockers(files, [".github/*"]))

    def test_no_patterns_or_no_matches_do_not_block(self):
        files = [{"filename": "src/app.py"}, {"filename": "docs/github.md"}]
        self.assertEqual(review_gate.path_blockers(files, []), [])
        self.assertEqual(review_gate.path_blockers(files, [".github/*"]), [])


class SyncApprovalTests(unittest.TestCase):
    def sync(self, client, review, before=HEAD, after=HEAD, excluded=(), **options):
        return review_gate.sync_approval(
            client, bot_login=BOT, head_sha_before=before, head_sha_after=after, review=review,
            excluded_patterns=excluded, **options)

    def test_changes_required_requests_changes_when_enabled(self):
        client = FakeClient([review_record(1, BOT, "APPROVED", OLD)])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"),
                           request_changes=True)
        self.assertEqual(result, "changes-requested")
        self.assertEqual([sha for sha, _ in client.requested], [HEAD])
        self.assertIn("changes_required", client.requested[0][1])
        self.assertEqual([review_id for review_id, _ in client.dismissed], [1])

    def test_changes_are_not_requested_twice_for_the_same_commit(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", HEAD)])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"),
                           request_changes=True)
        self.assertEqual(result, "already-changes-requested")
        self.assertEqual((client.requested, client.dismissed), ([], []))

    def test_changes_are_not_requested_by_default(self):
        client = FakeClient()
        result = self.sync(client, clean_review(merge_recommendation="changes_required"))
        self.assertEqual(result, "not-approved")
        self.assertEqual(client.requested, [])

    def test_softer_verdict_withdraws_the_bots_change_request(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD),
                             review_record(2, "alice", "CHANGES_REQUESTED", OLD)])
        result = self.sync(client, clean_review(merge_recommendation="needs_review"),
                           request_changes=True)
        self.assertEqual(result, "not-approved")
        self.assertEqual([review_id for review_id, _ in client.dismissed], [1])

    def test_failed_review_keeps_the_bots_change_request(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD),
                             review_record(2, BOT, "APPROVED", OLD)])
        result = self.sync(client, None, request_changes=True)
        self.assertEqual(result, "not-approved")
        self.assertEqual([review_id for review_id, _ in client.dismissed], [2])

    def test_clean_review_approval_supersedes_the_bots_change_request(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD)])
        self.assertEqual(self.sync(client, clean_review(), request_changes=True), "approved")
        self.assertEqual(client.dismissed, [])

    def test_approval_disabled_never_approves_but_still_requests_changes(self):
        client = FakeClient()
        self.assertEqual(self.sync(client, clean_review(), approve=False, request_changes=True),
                         "no-concerns")
        self.assertEqual(client.approved, [])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"),
                           approve=False, request_changes=True)
        self.assertEqual(result, "changes-requested")

    def test_clean_review_of_a_protected_path_withdraws_instead_of_approving(self):
        client = FakeClient([review_record(1, BOT, "APPROVED", OLD)],
                            files=[{"filename": ".github/workflows/review.yml"}])
        result = self.sync(client, clean_review(), excluded=[".github/*"])
        self.assertEqual(result, "not-approved")
        self.assertEqual(client.approved, [])
        self.assertEqual([review_id for review_id, _ in client.dismissed], [1])
        self.assertIn(".github/workflows/review.yml", client.dismissed[0][1])

    def test_truncated_file_list_never_approves_when_paths_are_protected(self):
        # GitHub lists at most 3000 files; a protected file could be among the missing ones.
        client = FakeClient(files=[{"filename": "src/app.py"}], changed_files=3001)
        result = self.sync(client, clean_review(), excluded=[".github/*"])
        self.assertEqual(result, "not-approved")
        self.assertEqual(client.approved, [])

    def test_clean_review_outside_protected_paths_is_approved(self):
        client = FakeClient(files=[{"filename": "src/app.py"}])
        self.assertEqual(self.sync(client, clean_review(), excluded=[".github/*"]), "approved")

    def test_clean_review_approves_the_reviewed_commit(self):
        client = FakeClient()
        result = self.sync(client, clean_review())
        self.assertEqual(result, "approved")
        self.assertEqual([sha for sha, _ in client.approved], [HEAD])

    def test_does_not_approve_the_same_commit_twice(self):
        client = FakeClient([review_record(1, BOT, "APPROVED", HEAD)])
        self.assertEqual(self.sync(client, clean_review()), "already-approved")
        self.assertEqual(client.approved, [])

    def test_head_change_during_review_leaves_reviews_untouched(self):
        client = FakeClient([review_record(1, BOT, "APPROVED", OLD)])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"), after=OLD)
        self.assertEqual(result, "head-changed")
        self.assertEqual((client.approved, client.dismissed), ([], []))

    def test_blocked_review_dismisses_only_the_bots_earlier_approvals(self):
        client = FakeClient([
            review_record(1, BOT, "APPROVED", OLD),
            review_record(2, "alice", "APPROVED", OLD),
            review_record(3, BOT, "COMMENTED", HEAD),
            review_record(4, BOT, "DISMISSED", OLD),
        ])
        result = self.sync(client, clean_review(merge_recommendation="needs_review"))
        self.assertEqual(result, "not-approved")
        self.assertEqual([review_id for review_id, _ in client.dismissed], [1])
        self.assertEqual(client.approved, [])

    def test_approval_on_new_commit_supersedes_older_bot_approval_without_dismissal(self):
        client = FakeClient([review_record(1, BOT, "APPROVED", OLD)])
        self.assertEqual(self.sync(client, clean_review()), "approved")
        self.assertEqual(client.dismissed, [])


if __name__ == "__main__":
    unittest.main()
