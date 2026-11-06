"""Reviews of one pull request end: a point already answered, a finding that did not block, a file
the model cannot see and a long run of reviews never keep approval blocked round after round."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_gate  # noqa: E402
from test_cli import BOT, HEAD, OLD, TOO_LARGE, ApiTestCase, CLEAN  # noqa: E402

LOGIN = BOT.removesuffix("[bot]")
BEFORE = "2026-05-01T00:00:00Z"
STARTED = "2026-06-01T00:00:00Z"


def finding(path="a.py", line=2, header="Duplicate asset IDs", content="c"):
    return {"relevant_file": path, "start_line": line, "end_line": line,
            "issue_header": header, "issue_content": content}


def thread(thread_id="T1", path="a.py", line=2, header="Duplicate asset IDs", resolved=False,
           resolved_by=None, created=BEFORE, author=LOGIN):
    return {"id": thread_id, "isResolved": resolved, "author": author, "created_at": created,
            "path": path, "original_start": line, "original_end": line, "resolved_by": resolved_by,
            "title": header, "url": f"https://github.com/o/r/pull/7#discussion_{thread_id}"}


def review_of(*issues, recommendation="changes_required"):
    review = json.loads(json.dumps(CLEAN))
    review["review"].update(merge_recommendation=recommendation, key_issues_to_review=list(issues))
    return review


class SamePointTests(unittest.TestCase):
    def test_a_title_in_other_words_is_the_same_point(self):
        for one, other in (("Duplicate asset IDs", "Duplicate asset IDs"),
                           ("Dataset Version Mismatch", "Dataset ID mismatch"),
                           ("Locale-dependent JSONL", "Locale-dependent decode")):
            with self.subTest(one=one):
                self.assertTrue(review_gate.same_point(one, other))

    def test_generic_or_different_titles_are_not(self):
        for one, other in (("Possible Issue", "Possible Bug"), ("Redaction bypass", "Secret redaction miss"),
                           ("", "Duplicate asset IDs"), ("Duplicate asset IDs", None)):
            with self.subTest(one=one):
                self.assertFalse(review_gate.same_point(one, other))

    def test_the_title_comes_from_the_bots_comment(self):
        self.assertEqual(review_gate.finding_title("**Duplicate asset IDs**\n\nSeveral ..."),
                         "Duplicate asset IDs")
        self.assertEqual(review_gate.finding_title("no title here"), "")


class RepeatedFindingTests(unittest.TestCase):
    def test_a_point_already_raised_and_open_or_answered_by_a_person_is_a_repeat(self):
        issue = finding()
        for earlier in (thread(), thread(resolved=True, resolved_by="alice")):
            with self.subTest(earlier=earlier["resolved_by"]):
                repeats = review_gate.repeated_findings([issue], [earlier], bot_login=BOT, started=STARTED)
                self.assertIs(repeats[id(issue)], earlier)

    def test_a_point_the_bot_resolved_as_fixed_or_another_file_or_this_run_is_not(self):
        issue = finding()
        for earlier in (thread(resolved=True, resolved_by=LOGIN), thread(path="b.py"),
                        thread(created=STARTED), thread(author="alice"), thread(header="Other thing")):
            with self.subTest(earlier=earlier):
                self.assertEqual(review_gate.repeated_findings([issue], [earlier], bot_login=BOT,
                                                               started=STARTED), {})


class FakeFiles:
    def __init__(self, files):
        self.files = files

    def file_at(self, path, ref):
        return self.files.get((path, ref))


class ConvergeStateTests(unittest.TestCase):
    def state(self, review, threads, reviews=1, limit=3, advisory=(), left_out=()):
        client = FakeFiles({("a.py", OLD): "x\ny\n", ("a.py", HEAD): "x\nY\n"})
        client.review_threads = lambda: threads
        return review_gate.converge_state(client, review, previous_head=OLD, head=HEAD, bot_login=BOT,
                                          started=STARTED, reviews=reviews, max_blocking=limit,
                                          advisory=set(advisory), left_out=set(left_out))

    def test_a_repeat_on_changed_lines_does_not_block(self):
        issue = finding()
        state = self.state(review_of(issue), [thread(resolved=True, resolved_by="alice")])
        self.assertEqual(state["blocking"], [])
        self.assertEqual([i for i, _ in state["repeats"]], [issue])

    def test_a_new_point_on_changed_lines_still_blocks(self):
        issue = finding(header="Off by one")
        state = self.state(review_of(issue), [thread(resolved=True, resolved_by="alice")])
        self.assertEqual(state["blocking"], [issue])

    def test_after_the_last_blocking_review_no_finding_blocks(self):
        issue = finding(header="Off by one")
        self.assertTrue(self.state(review_of(issue), [], reviews=3)["capped"])
        self.assertEqual(self.state(review_of(issue), [], reviews=3)["blocking"], [])
        self.assertEqual(self.state(review_of(issue), [], reviews=2)["blocking"], [issue])
        self.assertEqual(self.state(review_of(issue), [], reviews=50, limit=0)["blocking"], [issue])

    def test_threads_that_did_not_block_or_sit_on_files_left_out_are_not_open_findings(self):
        threads = [thread("T1"), thread("T2", line=9), thread("T3", path="data.json")]
        state = self.state(review_of(), threads, advisory={"T2"}, left_out={"data.json"})
        self.assertEqual([t["id"] for t in state["earlier"]], ["T1"])


class AdvisoryThreadTests(unittest.TestCase):
    def test_threads_this_run_opened_for_findings_that_do_not_block_are_advisory(self):
        blocking, other = finding(line=2, header="New"), finding(line=7, header="Old")
        threads = [thread("N1", line=2, created=STARTED), thread("N2", line=7, created=STARTED),
                   thread("E1", line=7)]  # E1 is from an earlier run.
        self.assertEqual(review_gate.advisory_threads([blocking, other], [blocking], threads,
                                                      bot_login=BOT, started=STARTED), {"N2"})

    def test_a_thread_that_also_covers_a_blocking_finding_is_not_advisory(self):
        one, two = finding(line=2, header="A"), finding(line=2, header="B")
        threads = [thread("N1", line=2, created=STARTED)]
        self.assertEqual(review_gate.advisory_threads([one, two], [one], threads, bot_login=BOT,
                                                      started=STARTED), set())


class StateMarkerTests(unittest.TestCase):
    def test_the_result_comment_keeps_the_review_count_and_advisory_threads(self):
        body = review_gate.result_comment(head_sha=HEAD, result="approved", blockers=[], review=CLEAN,
                                          skipped=[], reviewed_head=HEAD, reviews=4, advisory={"T2", "T1"})
        state = review_gate.parse_state(body)
        self.assertEqual(state, {"head": HEAD, "reviews": 4, "advisory": {"T1", "T2"}})

    def test_a_stale_comment_keeps_them_too(self):
        body = review_gate.stale_comment("review", HEAD, reviews=2, advisory={"T9"})
        self.assertEqual(review_gate.parse_state(body), {"head": HEAD, "reviews": 2, "advisory": {"T9"}})

    def test_no_comment_means_nothing_is_known(self):
        self.assertEqual(review_gate.parse_state(""), {"head": "", "reviews": 0, "advisory": set()})

    def test_the_comment_explains_a_finding_that_repeats_or_comes_after_the_last_blocking_review(self):
        repeat, late = finding(header="Duplicate asset IDs"), finding(line=5, header="Late")
        body = review_gate.result_comment(
            head_sha=HEAD, result="approved", blockers=[], review=review_of(repeat, late), skipped=[],
            blocking=[], repeats=[(repeat, thread())], reviews=4, max_blocking=3)
        _, _, rest = body.partition("**Not blocking**")
        self.assertIn("repeats [an earlier finding](https://github.com/o/r/pull/7#discussion_T1)", rest)
        self.assertIn("max-blocking-reviews", body)

    def test_files_too_large_to_review_are_named_for_a_person(self):
        body = review_gate.result_comment(head_sha=HEAD, result="not-approved", review=CLEAN, skipped=[],
                                          blockers=["1 file(s) too large to review"], too_large=["big.rs"])
        self.assertIn("`big.rs`", body)
        self.assertIn("too large", body)


class LargeFileTests(unittest.TestCase):
    class Contents:
        def __init__(self, readable):
            self.readable_map = readable
            self.asked = []

        def readable(self, path, ref):
            self.asked.append((path, ref))
            value = self.readable_map.get((path, ref), True)
            if isinstance(value, Exception):
                raise value
            return value

    def test_a_file_without_a_patch_that_github_cannot_serve_is_too_large(self):
        files = [{"filename": "big.rs", "status": "modified", "changes": 4000},
                 {"filename": "ok.rs", "status": "modified", "changes": 4000},
                 {"filename": "small.rs", "status": "modified", "changes": 3, "patch": "@@"},
                 {"filename": "logo.png", "status": "added", "changes": 0},
                 {"filename": "gone.rs", "status": "removed", "changes": 9000}]
        client = self.Contents({("big.rs", HEAD): False, ("gone.rs", OLD): False})
        self.assertEqual(review_gate.too_large_files(client, files, base=OLD, head=HEAD),
                         ["big.rs", "gone.rs"])
        self.assertNotIn(("gone.rs", HEAD), client.asked)  # A removed file has no head version.

    def test_a_renamed_file_is_read_at_its_old_name_and_a_read_error_keeps_it_reviewed(self):
        files = [{"filename": "new.rs", "previous_filename": "old.rs", "status": "renamed", "changes": 9},
                 {"filename": "flaky.rs", "status": "modified", "changes": 9}]
        client = self.Contents({("old.rs", OLD): False,
                                ("flaky.rs", HEAD): review_gate.GitHubError("502", status=502)})
        self.assertEqual(review_gate.too_large_files(client, files, base=OLD, head=HEAD), ["new.rs"])


class LoopCommandTests(ApiTestCase):
    def comment(self, fake, reviews, advisory=()):
        markers = [f"<!-- deepseek-review:head {OLD} -->", f"<!-- deepseek-review:reviews {reviews} -->"]
        if advisory:
            markers.append(f"<!-- deepseek-review:advisory {' '.join(advisory)} -->")
        fake.comments.append({"id": 1, "user": {"login": BOT},
                              "body": "\n".join(["<!-- deepseek-review:result -->", *markers])})
        fake.contents = {("a.py", OLD): "same\n", ("a.py", HEAD): "same\nnew\n"}

    def approve(self, fake, review, **extra):
        env = self.api_env(fake, BOT_LOGIN=BOT, HEAD_SHA_BEFORE=HEAD, REVIEW_JSON=self.write_review(review),
                           APPROVAL_EXCLUDED_PATHS="", RESULT_COMMENT="true", RUNNER_TEMP=str(self.tmp),
                           RUN_STARTED=STARTED, REQUEST_CHANGES="true", **extra)
        return self.run_cli("approve", **env)

    def test_each_decided_review_is_counted_in_the_result_comment(self):
        fake = self.start()
        self.comment(fake, reviews=1)
        result = self.approve(fake, review_of(finding(header="Off by one")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<!-- deepseek-review:reviews 2 -->", fake.comments[0]["body"])

    def test_after_the_last_blocking_review_findings_on_new_code_no_longer_block(self):
        fake = self.start()
        self.comment(fake, reviews=3)
        result = self.approve(fake, review_of(finding(header="Off by one")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Approval result: approved", result.stdout)
        self.assertIn("max-blocking-reviews", fake.comments[0]["body"])

    def test_the_limit_can_be_turned_off(self):
        fake = self.start()
        self.comment(fake, reviews=3)
        result = self.approve(fake, review_of(finding(header="Off by one")), MAX_BLOCKING_REVIEWS="0")
        self.assertIn("changes-requested", result.stdout)

    def test_an_invalid_limit_is_refused_before_any_change(self):
        fake = self.start()
        result = self.approve(fake, CLEAN, MAX_BLOCKING_REVIEWS="many")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(fake.writes(), [])

    def test_a_thread_for_a_finding_that_did_not_block_never_blocks_a_later_review(self):
        fake = self.start()
        self.comment(fake, reviews=1, advisory=["T7"])
        fake.threads = [{"id": "T7", "isResolved": False, "author": LOGIN, "createdAt": BEFORE,
                         "path": "a.py", "originalLine": 1, "commit": OLD}]
        result = self.approve(fake, CLEAN)
        self.assertIn("Approval result: approved", result.stdout)
        self.assertIn("<!-- deepseek-review:advisory T7 -->", fake.comments[0]["body"])

    def test_the_thread_this_run_opens_for_a_finding_that_does_not_block_is_remembered(self):
        fake = self.start()
        self.comment(fake, reviews=1)
        old_code = finding(line=1, header="Old point")  # Line 1 was already reviewed.
        fake.threads = [{"id": "N1", "isResolved": False, "author": LOGIN, "createdAt": "2026-06-01T00:00:05Z",
                         "path": "a.py", "originalLine": 1, "commit": HEAD}]
        result = self.approve(fake, review_of(old_code))
        self.assertIn("Approval result: approved", result.stdout)
        self.assertIn("<!-- deepseek-review:advisory N1 -->", fake.comments[0]["body"])

    def test_a_point_a_person_answered_does_not_block_when_raised_again_on_new_code(self):
        fake = self.start()
        self.comment(fake, reviews=1)
        fake.threads = [{"id": "T1", "isResolved": True, "resolvedBy": "alice", "author": LOGIN,
                         "createdAt": BEFORE, "path": "a.py", "originalLine": 1, "commit": OLD,
                         "body": "**Duplicate asset IDs**\n\nSeveral IDs repeat."}]
        result = self.approve(fake, review_of(finding(header="Duplicate asset ids")))
        self.assertIn("Approval result: approved", result.stdout)
        self.assertIn("repeats", fake.comments[0]["body"])

    def test_a_file_too_large_to_review_blocks_approval_and_is_named(self):
        fake = self.start()
        (self.tmp / "deepseek-review").mkdir()
        (self.tmp / "deepseek-review" / "too-large-files.json").write_text(json.dumps(["big.rs"]))
        result = self.approve(fake, CLEAN)
        self.assertIn("Not approving: 1 file(s) too large", result.stdout)
        self.assertIn("`big.rs`", fake.comments[0]["body"])

    def test_the_data_files_step_leaves_files_too_large_to_review_out(self):
        fake = self.start(files=[{"filename": "big.rs", "status": "modified", "changes": 4000},
                                 {"filename": "a.py", "status": "modified", "changes": 1, "patch": "@@"}])
        fake.contents = {("big.rs", HEAD): TOO_LARGE, ("big.rs", fake.base_sha): "fn main() {}\n"}
        result = self.run_cli("data-files", **self.api_env(fake, RUNNER_TEMP=str(self.tmp)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.outputs()["ignore-globs"]), ["big.rs"])
        self.assertEqual(json.loads((self.tmp / "deepseek-review" / "too-large-files.json").read_text()),
                         ["big.rs"])
        note = (self.tmp / "deepseek-review" / "left-out.txt").read_text()
        self.assertIn("`big.rs`", note)
        self.assertIn("Do not report them", note)

    def test_a_file_the_users_globs_leave_out_is_never_called_too_large(self):
        fake = self.start(files=[{"filename": "big.rs", "status": "modified", "changes": 4000}])
        fake.contents = {("big.rs", HEAD): TOO_LARGE}
        result = self.run_cli("data-files", **self.api_env(fake, RUNNER_TEMP=str(self.tmp),
                                                           IGNORE_GLOBS='["*.rs"]'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.tmp / "deepseek-review" / "too-large-files.json").read_text()), [])
        self.assertEqual((self.tmp / "deepseek-review" / "left-out.txt").read_text(), "")

    def test_head_sha_reports_how_many_reviews_came_before(self):
        fake = self.start()
        self.comment(fake, reviews=2)
        result = self.run_cli("head-sha", **self.api_env(fake, BOT_LOGIN=BOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["reviews"], "2")

    def test_a_stale_run_keeps_the_count_and_the_advisory_threads(self):
        fake = self.start()
        self.comment(fake, reviews=2, advisory=["T7"])
        result = self.run_cli("stale", **self.api_env(fake, BOT_LOGIN=BOT, RESULT_COMMENT="true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        state = review_gate.parse_state(fake.comments[0]["body"])
        self.assertEqual((state["reviews"], state["advisory"]), (2, {"T7"}))


if __name__ == "__main__":
    unittest.main()
