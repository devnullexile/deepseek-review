import json
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

    def test_members_only_reviews_organization_members_whatever_their_association(self):
        members = {"alice"}
        for association in ("MEMBER", "CONTRIBUTOR", "NONE"):
            with self.subTest(association=association):
                allowed, _ = review_gate.author_allowed(
                    pull_request(login="Alice", association=association), [],
                    is_member=lambda login: login.lower() in members)
                self.assertTrue(allowed)

    def test_members_only_refuses_collaborators_outside_the_organization(self):
        allowed, reason = review_gate.author_allowed(
            pull_request(login="carol", association="COLLABORATOR"), [], is_member=lambda login: False)
        self.assertFalse(allowed)
        self.assertIn("not a member", reason)

    def test_members_only_still_admits_explicitly_listed_authors(self):
        allowed, _ = review_gate.author_allowed(
            pull_request(login="helper-bot", association="NONE"), ["helper-bot"],
            is_member=lambda login: False)
        self.assertTrue(allowed)

    def test_members_only_skips_drafts_without_asking_github(self):
        def fail(login):
            raise AssertionError("membership must not be checked for drafts")
        allowed, _ = review_gate.author_allowed(pull_request(draft=True), [], is_member=fail)
        self.assertFalse(allowed)

    def test_members_only_refuses_an_unknown_author_without_asking_github(self):
        def fail(login):
            raise AssertionError("membership must not be checked for an empty login")
        pr = pull_request()
        pr["user"] = None  # deleted account
        allowed, reason = review_gate.author_allowed(pr, [], is_member=fail)
        self.assertFalse(allowed)
        self.assertIn("unknown", reason)

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

    def test_changes_required_keeps_an_existing_request_when_the_option_is_off(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD)])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"))
        self.assertEqual(result, "not-approved")
        self.assertEqual((client.requested, client.dismissed), ([], []))

    def test_failed_review_keeps_the_bots_change_request(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD),
                             review_record(2, BOT, "APPROVED", OLD)])
        result = self.sync(client, None, request_changes=True)
        self.assertEqual(result, "not-approved")
        self.assertEqual([review_id for review_id, _ in client.dismissed], [2])

    def test_clean_review_approves_and_withdraws_the_bots_change_request(self):
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", OLD)])
        self.assertEqual(self.sync(client, clean_review(), request_changes=True), "approved")
        self.assertEqual([review_id for review_id, _ in client.dismissed], [1])

    def test_request_superseded_by_a_later_approval_is_posted_again(self):
        # Reviews are listed oldest first; the approval on the same head is the current verdict.
        client = FakeClient([review_record(1, BOT, "CHANGES_REQUESTED", HEAD),
                             review_record(2, BOT, "APPROVED", HEAD)])
        result = self.sync(client, clean_review(merge_recommendation="changes_required"),
                           request_changes=True)
        self.assertEqual(result, "changes-requested")
        self.assertEqual([sha for sha, _ in client.requested], [HEAD])
        self.assertIn(2, [review_id for review_id, _ in client.dismissed])

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



class DataFileTests(unittest.TestCase):
    def changed(self, name, additions=10, patch=True):
        item = {"filename": name, "additions": additions, "changes": additions}
        if patch:
            item["patch"] = "@@"
        return item

    def test_lock_files_and_data_formats_always_count_wherever_they_are(self):
        for name in ("uv.lock", "web/package-lock.json", "go.sum", "runs/out.jsonl",
                     "data/table.csv", "static/app.min.js"):
            with self.subTest(name=name):
                self.assertEqual(review_gate.data_files([self.changed(name, 3)], max_lines=1000), [name])

    def test_structured_files_count_only_when_large_or_without_a_patch(self):
        files = [self.changed("package.json", 40), self.changed("evidence/runs.json", 5000),
                 self.changed("config.yaml", 20, patch=False), self.changed("src/app.py", 9000)]
        self.assertEqual(review_gate.data_files(files, max_lines=1000),
                         ["evidence/runs.json", "config.yaml"])

    def test_a_path_a_person_approves_is_always_reviewed(self):
        files = [self.changed(".github/workflows/big.yml", 5000), self.changed("uv.lock")]
        self.assertEqual(review_gate.data_files(files, max_lines=1000, protected=[".github/*"]),
                         ["uv.lock"])

    def test_each_skipped_file_becomes_an_exact_glob_after_the_users_own(self):
        globs, kept = review_gate.ignore_list(["*.meta"], ["data/[v2]*.json", "a.lock"])
        self.assertEqual(globs, ["*.meta", "data/[[]v2][*].json", "a.lock"])
        self.assertEqual(kept, ["data/[v2]*.json", "a.lock"])

    def test_the_ignore_list_stays_inside_the_environment_limit(self):
        many = [f"data/{n:05}-{'x' * 80}.jsonl" for n in range(3000)]
        globs, kept = review_gate.ignore_list([], many)
        self.assertLess(len(json.dumps(globs)), review_gate.MAX_IGNORE_CHARS + 200)
        self.assertLess(len(kept), len(many))
        self.assertEqual(globs, kept)


class ReviewTriggerTests(unittest.TestCase):
    def wanted(self, action, trigger="push", label_name=None):
        event = {"action": action}
        if label_name is not None:
            event["label"] = {"name": label_name}
        return review_gate.review_wanted(event, trigger=trigger, label="review")[:3]

    def test_push_mode_reviews_every_commit(self):
        self.assertEqual(self.wanted("synchronize"), (True, False, False))
        self.assertEqual(self.wanted("opened"), (True, False, False))

    def test_label_mode_reviews_new_commits_only_on_request(self):
        self.assertEqual(self.wanted("synchronize", "label"), (False, True, False))
        for action in ("opened", "reopened", "ready_for_review"):
            with self.subTest(action=action):
                self.assertEqual(self.wanted(action, "label"), (True, False, False))
        retarget = {"action": "edited", "changes": {"base": {"ref": {"from": "main"}}}}
        self.assertTrue(review_gate.review_wanted(retarget, trigger="label", label="review")[0])

    def test_the_review_label_asks_for_a_review_in_either_mode(self):
        for trigger in ("push", "label"):
            with self.subTest(trigger=trigger):
                self.assertEqual(self.wanted("labeled", trigger, "Review"), (True, False, True))
                self.assertEqual(self.wanted("labeled", trigger, "bug"), (False, False, False))


class ResultCommentTests(unittest.TestCase):
    def test_it_names_every_reason_finding_and_left_out_file(self):
        review = clean_review(merge_recommendation="changes_required", key_issues_to_review=[
            {"relevant_file": "src/a.py", "start_line": 12, "issue_header": "Possible Bug",
             "issue_content": "x is None when the list is empty"}])
        review["coverage"]["files_not_reviewed"] = ["docs/big.md"]
        body = review_gate.result_comment(
            head_sha=HEAD, result="not-approved", review=review, skipped=["uv.lock"],
            blockers=["merge recommendation is 'changes_required'", "1 key issue(s) reported"])
        self.assertTrue(body.startswith(review_gate.RESULT_MARKER))
        for text in ("Not approved", "aaaaaaa", "changes_required", "`src/a.py:12`", "Possible Bug",
                     "x is None when the list is empty", "`docs/big.md`", "`uv.lock`"):
            self.assertIn(text, body)

    def test_a_clean_review_says_nothing_blocks_approval(self):
        body = review_gate.result_comment(head_sha=HEAD, result="approved", review=clean_review(),
                                          skipped=[], blockers=[])
        self.assertIn("Approved", body)
        self.assertIn("nothing that blocks approval", body)

    def test_model_text_cannot_mention_people_or_end_the_marker(self):
        review = clean_review(key_issues_to_review=[{"relevant_file": "a", "issue_header": "h",
                                                     "issue_content": "ask @alice --> <!-- x"}])
        body = review_gate.result_comment(head_sha=HEAD, result="not-approved", review=review,
                                          skipped=[], blockers=["1 key issue(s) reported"])
        self.assertNotIn("@alice", body)
        self.assertEqual(body.count("<!--"), 1)

    def test_a_review_overtaken_by_new_commits_decides_nothing(self):
        body = review_gate.result_comment(head_sha=HEAD, result="head-changed", review=None,
                                          skipped=[], blockers=[])
        self.assertIn("Not decided", body)


class PostResultTests(unittest.TestCase):
    class Comments:
        def __init__(self, comments):
            self.comments = comments
            self.created, self.updated = [], []

        def list_comments(self):
            return self.comments

        def create_comment(self, body):
            self.created.append(body)

        def update_comment(self, comment_id, body):
            self.updated.append((comment_id, body))

    def test_the_bots_own_comment_is_updated_not_repeated(self):
        client = self.Comments([
            {"id": 1, "user": {"login": "alice"}, "body": review_gate.RESULT_MARKER + " copied"},
            {"id": 2, "user": {"login": BOT}, "body": review_gate.RESULT_MARKER + " old"}])
        review_gate.post_result(client, BOT, review_gate.RESULT_MARKER + " new")
        self.assertEqual(client.updated, [(2, review_gate.RESULT_MARKER + " new")])
        self.assertEqual(client.created, [])

    def test_the_first_result_is_posted(self):
        client = self.Comments([])
        review_gate.post_result(client, BOT, review_gate.RESULT_MARKER + " new")
        self.assertEqual(client.created, [review_gate.RESULT_MARKER + " new"])


class MalformedReviewCommentTests(unittest.TestCase):
    def test_a_readable_but_malformed_review_still_gets_a_result_comment(self):
        for review in ({"review": []}, {"review": {"key_issues_to_review": "x"}, "coverage": []},
                       {"review": {"key_issues_to_review": [None, "x"]}, "coverage": {"files_not_reviewed": "a"}},
                       [], "text"):
            with self.subTest(review=review):
                body = review_gate.result_comment(head_sha=HEAD, result="not-approved", review=review,
                                                  skipped=[], blockers=["review output is missing or malformed"])
                self.assertIn("Not approved", body)


class ResultCommentEscapingTests(unittest.TestCase):
    def test_untrusted_text_renders_as_text_not_markdown(self):
        review = clean_review(key_issues_to_review=[{
            "relevant_file": "a`b.py", "issue_header": "**Bold**",
            "issue_content": "[approve here](https://evil.example) _x_ <b>"}])
        body = review_gate.result_comment(head_sha=HEAD, result="not-approved", review=review,
                                          skipped=["x` [click](https://evil.example)`.csv"],
                                          blockers=["1 key issue(s) reported"])
        # The finding's link is escaped; the file name stays inside one code span.
        self.assertNotIn("[approve here](", body)
        self.assertIn("`xʼ [click](https://evil.example)ʼ.csv`", body)
        self.assertNotIn("x` [click]", body)
        self.assertIn("\\[approve here\\]", body)


class EditedEventTests(unittest.TestCase):
    def test_only_a_retarget_counts_as_an_edit_in_either_mode(self):
        for trigger in ("push", "label"):
            with self.subTest(trigger=trigger):
                retarget = {"action": "edited", "changes": {"base": {"ref": {"from": "main"}}}}
                title = {"action": "edited", "changes": {"title": {"from": "old"}}}
                self.assertTrue(review_gate.review_wanted(retarget, trigger=trigger, label="review")[0])
                self.assertFalse(review_gate.review_wanted(title, trigger=trigger, label="review")[0])


class OtherEventTests(unittest.TestCase):
    def test_only_the_documented_events_start_a_review(self):
        for action in ("unlabeled", "assigned", "review_requested", "converted_to_draft", "closed"):
            for trigger in ("push", "label"):
                with self.subTest(action=action, trigger=trigger):
                    wanted, stale, _, _ = review_gate.review_wanted(
                        {"action": action}, trigger=trigger, label="review")
                    self.assertFalse(wanted or stale)

if __name__ == "__main__":
    unittest.main()
