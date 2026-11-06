"""Gate and approval logic for the DeepSeek pull request review action."""

import base64
import datetime
import difflib
import fnmatch
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WRITER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
NEGATIVE_SECURITY_ANSWERS = frozenset({"no", "none", "false"})
# Used when APPROVAL_EXCLUDED_PATHS is unset; only an explicitly empty value approves any path.
DEFAULT_EXCLUDED_PATHS = ".github/*"

# Files that hold data, not code: a model cannot review them usefully, and a large one fills the
# token window so the code is left unreviewed. Matched against the file name, not its folder.
DATA_FILE_NAMES = (
    "*.jsonl", "*.ndjson", "*.csv", "*.tsv", "*.min.js", "*.min.css", "*.map",
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "Cargo.lock", "go.sum",
    "composer.lock", "Gemfile.lock", "packages.lock.json", "flake.lock",
)
# Structured data that is code-like when small (package.json, a workflow) and data when large.
STRUCTURED_DATA_SUFFIXES = (".json", ".yaml", ".yml", ".xml", ".toml")
# GitHub limits one environment value to about 128 KB; the ignore list stays well inside it.
MAX_IGNORE_CHARS = 100_000
RESULT_MARKER = "<!-- deepseek-review:result -->"
# The head of the last decided review, kept in the result comment, so a later review knows what
# it already saw.
HEAD_MARKER_RE = re.compile(r"<!-- deepseek-review:head ([0-9a-f]{40}) -->")
REVIEW_TRIGGERS = ("push", "label")
# The pull request events that can ask for a review; any other, such as the bot removing its own
# label (unlabeled), starts none. A payload without an action is treated as before: reviewed.
REVIEW_ACTIONS = frozenset({"opened", "reopened", "ready_for_review", "synchronize", "edited", None})


def author_allowed(pull_request, allowed_authors, is_member=None):
    """Return (allowed, reason) for reviewing this pull request.

    With is_member (members-only mode), organization membership decides, plus any
    allowed-authors; author_association is not trusted, because GitHub may report a
    member with private membership as CONTRIBUTOR.
    """
    if pull_request.get("draft"):
        return False, "pull request is a draft"
    login = (pull_request.get("user") or {}).get("login", "")
    allowed = {name.strip().lower() for name in allowed_authors if name.strip()}
    if is_member is not None:
        if not login:
            return False, "pull request author is unknown (deleted account)"
        if login.lower() in allowed:
            return True, f"{login} is in allowed-authors"
        if is_member(login):
            return True, f"{login} is a member of the organization"
        return False, f"{login} is not a member of the organization"
    if allowed:
        if login.lower() in allowed:
            return True, f"{login} is in allowed-authors"
        return False, f"{login} is not in allowed-authors"
    association = pull_request.get("author_association", "NONE")
    if association in WRITER_ASSOCIATIONS:
        return True, f"{login} is {association}"
    return False, f"{login} is {association}; set allowed-authors to review outside contributors"


def _security_concern(value):
    # Same rule as pr-agent's is_value_no(), so approval agrees with the posted comment.
    # A bare YAML `No` answer arrives as False.
    if not value:
        return False
    return str(value).strip().lower() not in NEGATIVE_SECURITY_ANSWERS


def approval_blockers(output, blocking_issues=None):
    """Return reasons that prevent approval; an empty list means approve.

    The check fails closed: anything missing or unexpected blocks approval. With
    blocking_issues (a later review that converges), only those findings block, and the merge
    recommendation is advisory: it blocks only together with a blocking finding.
    """
    if not isinstance(output, dict) or not isinstance(output.get("review"), dict):
        return ["review output is missing or malformed"]
    review = output["review"]
    blockers = []

    recommendation = review.get("merge_recommendation")
    issues = review.get("key_issues_to_review")
    if blocking_issues is None:
        if recommendation != "no_concerns_found":
            blockers.append(f"merge recommendation is {recommendation!r}")
        if not isinstance(issues, list):
            blockers.append("key issues are missing")
        elif issues:
            blockers.append(f"{len(issues)} key issue(s) reported")
    elif not isinstance(issues, list):
        blockers.append("key issues are missing")
    elif blocking_issues:
        blockers.append(f"merge recommendation is {recommendation!r}")
        blockers.append(f"{len(blocking_issues)} finding(s) on lines changed since the last review")

    if _security_concern(review.get("security_concerns")):
        blockers.append("security concerns reported")

    coverage = output.get("coverage")
    if not isinstance(coverage, dict):
        blockers.append("review coverage is missing")
    else:
        for key, label in (("files_not_reviewed", "not reviewed"),
                           ("files_partially_reviewed", "partially reviewed")):
            files = coverage.get(key)
            if not isinstance(files, list):
                blockers.append(f"coverage field {key} is missing")
            elif files:
                blockers.append(f"{len(files)} file(s) {label}")
    return blockers


def path_blockers(files, patterns):
    """Block approval of changes to paths that need a human, such as CI workflows."""
    matched = []
    for changed in files:
        for name in (changed.get("filename"), changed.get("previous_filename")):
            if name and any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                matched.append(name)
                break
    if not matched:
        return []
    shown = ", ".join(matched[:3]) + (f" and {len(matched) - 3} more" if len(matched) > 3 else "")
    return [f"changes to {shown} need a human approval"]


def data_files(files, *, max_lines, protected=()):
    """Changed files to leave out of the model review as data, in the order GitHub lists them.

    A lock file, JSON Lines, CSV or minified file always counts. A JSON, YAML, XML or TOML file
    counts when more than max_lines lines were added, or when GitHub sends no patch for it
    because it is too large. A path a person must approve (protected) is always reviewed.
    """
    found = []
    for changed in files:
        name = changed.get("filename") or ""
        if not name or any(fnmatch.fnmatchcase(name, pattern) for pattern in protected):
            continue
        base = posixpath.basename(name)
        if any(fnmatch.fnmatchcase(base, pattern) for pattern in DATA_FILE_NAMES):
            found.append(name)
        elif base.lower().endswith(STRUCTURED_DATA_SUFFIXES) and (
            (changed.get("additions") or 0) > max_lines
            or ("patch" not in changed and (changed.get("changes") or 0) > 0)
        ):
            found.append(name)
    return found


def escape_glob(path):
    """A glob that matches exactly this path."""
    return re.sub(r"([*?\[])", r"[\1]", path)


def ignore_list(user_globs, skipped):
    """The globs pr-agent leaves out: the user's own, then each skipped data file, within the
    environment size limit. Returns (globs, the skipped files that fit)."""
    globs = list(user_globs)
    kept = []
    size = len(json.dumps(globs))
    for path in skipped:
        glob = escape_glob(path)
        size += len(json.dumps(glob)) + 2
        if size > MAX_IGNORE_CHARS:
            break
        globs.append(glob)
        kept.append(path)
    return globs, kept


def review_wanted(event, *, trigger, label):
    """(run, stale, label_requested, reason) for this pull request event.

    With trigger "push" every new commit is reviewed. With "label" a pull request is reviewed
    when it is opened, reopened, marked ready or retargeted, and later only when someone adds
    the review label; a push then only withdraws an approval of older commits (stale).
    In both modes, adding the review label asks for a review of the current commit.
    """
    action = event.get("action")
    if action == "labeled":
        name = ((event.get("label") or {}).get("name") or "").strip().lower()
        if name == label.strip().lower():
            return True, False, True, f"the {label!r} label asks for a review"
        return False, False, False, f"the {name!r} label does not ask for a review"
    if action not in REVIEW_ACTIONS:
        return False, False, False, f"a pull request {action!r} event asks for no review"
    if action == "edited" and not (event.get("changes") or {}).get("base"):
        return False, False, False, "a title or description edit asks for no review"
    if trigger == "label" and action == "synchronize":
        return False, True, False, f"new commits are reviewed when someone adds the {label!r} label"
    return True, False, False, f"the pull request was {action or 'updated'}"


def _one_line(text, limit):
    text = " ".join(str(text or "").split())
    # A comment must not end our marker or mention a person.
    text = text.replace("<!--", "< !--").replace("@", "@\u200b")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _text(text, limit):
    """Untrusted text as plain text: no link, image, emphasis, code span or HTML of its own."""
    return re.sub(r"([\\`*\[\]<>])", r"\\\1", _one_line(text, limit))


def _code(text, limit):
    """Untrusted text as one code span it cannot leave."""
    return "`" + _one_line(text, limit).replace("`", "\u02bc") + "`"


def _listed(names, limit=10):
    shown = [_code(name, 200) for name in names[:limit]]
    more = len(names) - limit
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


RESULT_TITLES = {
    "approved": "Approved", "already-approved": "Approved",
    "changes-requested": "Changes requested", "already-changes-requested": "Changes requested",
    "no-concerns": "No blocking concerns", "not-approved": "Not approved",
    "head-changed": "Not decided", "": "Reviewed",
}


_SUMMARIZED = re.compile(r"^\d+ (key issue\(s\) reported|finding\(s\) on lines changed|earlier finding\(s\))")


def _where(issue):
    where = str(issue.get("relevant_file") or "")
    lines = _issue_lines(issue)
    return where + (f":{lines[0]}" if lines else ""), lines


def _file_link(repo_url, head_sha, path, lines):
    if not repo_url or not path:
        return ""
    anchor = f"#L{lines[0]}" + (f"-L{lines[1]}" if lines and lines[1] != lines[0] else "") if lines else ""
    return f"{repo_url}/blob/{head_sha}/{urllib.parse.quote(path)}{anchor}"


def _linked(label, url):
    # Only links the bot builds itself, to this repository or to a thread GitHub reported.
    return f"[{label}]({url})" if url and url.startswith(("https://", "http://")) else label


def result_comment(*, head_sha, result, blockers, review, skipped, blocking=None, reviewed_head="",
                   resolved=0, earlier=(), repo_url=""):
    """The bot's own result comment: what it decided, and a checklist of what to change.

    blocking lists the findings that block approval in a converging review (None: all do).
    earlier lists the bot's open threads from before this run. reviewed_head is the head this
    decision covers, kept for the next review. repo_url, when set, links files and threads.
    """
    title = RESULT_TITLES.get(result, "Not approved")
    if result == "" and blockers:  # Comment-only mode: the title follows the blockers.
        title = "Not approved"
    lines = [RESULT_MARKER]
    if reviewed_head:
        lines.append(f"<!-- deepseek-review:head {reviewed_head} -->")
    lines += [f"### DeepSeek review: {title} (`{head_sha[:7]}`)", ""]
    output = review if isinstance(review, dict) else {}
    body = output.get("review") if isinstance(output.get("review"), dict) else {}
    issues = [i for i in body.get("key_issues_to_review") or [] if isinstance(i, dict)] \
        if isinstance(body.get("key_issues_to_review"), list) else []
    blocking_issues = issues if blocking is None else [i for i in issues if any(i is b for b in blocking)]
    other_issues = [i for i in issues if not any(i is b for b in blocking_issues)]
    if result == "head-changed":
        lines.append("New commits arrived during this review, so it decides nothing. "
                     "The next review of the latest commit will.")
    elif not blockers:
        lines.append("The review found nothing that blocks approval.")
    else:
        lines.append("**To get the bot's approval:**")
        for issue in blocking_issues[:10]:
            where, span = _where(issue)
            link = _file_link(repo_url, head_sha, issue.get("relevant_file"), span)
            lines.append(f"- [ ] Fix, or answer in its thread: {_linked(_code(where, 200), link)} "
                         f"**{_text(issue.get('issue_header'), 80)}**: {_text(issue.get('issue_content'), 300)}")
        if len(blocking_issues) > 10:
            lines.append(f"- [ ] And {len(blocking_issues) - 10} more findings in the review comment.")
        for thread in list(earlier)[:10]:
            where = str(thread.get("path") or "an earlier finding")
            if thread.get("original_end"):
                where += f":{thread.get('original_end')}"
            lines.append(f"- [ ] Fix it in a commit, or answer and resolve the thread: "
                         f"{_linked(_code(where, 200), thread.get('url') or '')}")
        if len(earlier) > 10:
            lines.append(f"- [ ] And {len(earlier) - 10} more open threads of the bot.")
        for blocker in blockers:
            if not _SUMMARIZED.match(blocker):
                lines.append(f"- [ ] {_text(blocker, 300)}")
    if other_issues:
        lines += ["", "**Not blocking**, raised again on code the previous review already saw:"]
        for issue in other_issues[:10]:
            where, span = _where(issue)
            link = _file_link(repo_url, head_sha, issue.get("relevant_file"), span)
            lines.append(f"- {_linked(_code(where, 200), link)} **{_text(issue.get('issue_header'), 80)}**: "
                         f"{_text(issue.get('issue_content'), 300)}")
    coverage = output.get("coverage") if isinstance(output.get("coverage"), dict) else {}
    unreviewed = [name for key in ("files_not_reviewed", "files_partially_reviewed")
                  if isinstance(coverage.get(key), list)
                  for name in coverage[key] if isinstance(name, str)]
    if unreviewed:
        lines += ["", f"The model could not review in full: {_listed(unreviewed)}."]
    if skipped:
        lines += ["", f"**For a person to check**, left out of the review as data files: {_listed(skipped)}."]
    if resolved:
        lines += ["", f"**Done:** resolved {resolved} earlier thread(s) of the bot whose lines a later "
                      "commit changed, with no finding raised there again."]
    lines += ["", "_This comment is updated on every review. The review comment and its threads have the details._"]
    return "\n".join(lines)


def stale_comment(label, reviewed_head=""):
    head = [f"<!-- deepseek-review:head {reviewed_head} -->"] if reviewed_head else []
    return "\n".join([
        RESULT_MARKER, *head, "### DeepSeek review: Not reviewed yet", "",
        f"New commits arrived after the last review. Add the `{label}` label to review them.",
        "", "_This comment is updated on every review._"])


def _own_result(client, bot_login):
    for comment in client.list_comments():
        if ((comment.get("user") or {}).get("login") == bot_login
                and (comment.get("body") or "").startswith(RESULT_MARKER)):
            return comment
    return None


def previous_head(client, bot_login):
    """The head the bot's last decided review covered, from its result comment, or ""."""
    comment = _own_result(client, bot_login)
    found = HEAD_MARKER_RE.search((comment or {}).get("body") or "")
    return found.group(1) if found else ""


def post_result(client, bot_login, body):
    """Update the bot's result comment, or post the first one."""
    for comment in client.list_comments():
        if ((comment.get("user") or {}).get("login") == bot_login
                and (comment.get("body") or "").startswith(RESULT_MARKER)):
            if comment.get("body") != body:
                client.update_comment(comment["id"], body)
            return
    client.create_comment(body)


def _recommendation(output):
    """The merge recommendation of a parsed review, or None when the review is unusable."""
    if not isinstance(output, dict) or not isinstance(output.get("review"), dict):
        return None
    return output["review"].get("merge_recommendation")


def changed_lines(old_text, new_text):
    """Line numbers (from 1) of new_text that were added or changed since old_text."""
    old, new = old_text.splitlines(), new_text.splitlines()
    changed = set()
    for tag, _, _, start, end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            changed.update(range(start + 1, end + 1))
    return changed


def _issue_lines(issue):
    try:
        start = int(issue.get("start_line") or 0)
        end = int(issue.get("end_line") or start)
    except (TypeError, ValueError):
        return None
    return (start, max(start, end)) if start > 0 else None


def blocking_findings(client, review, previous_head, head):
    """The findings of this review that block approval when it converges.

    A finding blocks when it is on a line added or changed since the previous review, because
    that code was not reviewed before. It also blocks, failing closed, when its file or lines
    cannot be read. A finding on code the previous review already saw does not block: it was
    reported then, and an earlier thread that is still open blocks on its own.
    """
    issues = ((review or {}).get("review") or {}).get("key_issues_to_review")
    if not isinstance(issues, list):
        return None
    changed = {}
    blocking = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("relevant_file"), str):
            blocking.append(issue)
            continue
        lines = _issue_lines(issue)
        path = issue["relevant_file"]
        if path not in changed:
            try:
                old, new = client.file_at(path, previous_head), client.file_at(path, head)
                changed[path] = None if old is None or new is None else changed_lines(old, new)
            except GitHubError:
                changed[path] = None
        lines_changed = changed[path]
        if lines is None or lines_changed is None or lines_changed & set(range(lines[0], lines[1] + 1)):
            blocking.append(issue)
    return blocking


def touched(old_text, new_text, start, end):
    """Whether lines start..end (from 1) of old_text were changed or removed in new_text, and
    where that region now is in new_text as (first, last) line numbers."""
    hit, where = False, []
    first, last = start - 1, end  # The region as 0-based [first, last).
    matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i1 < last and i2 > first:  # This block covers part of the region.
            if tag in ("replace", "delete"):
                hit = True
                where += [j1 + 1, max(j1 + 1, j2)]
            else:  # Equal lines keep their place, shifted by the lines before them.
                where += [j1 + 1 + max(first, i1) - i1, j1 + min(last, i2) - i1]
    return hit, ((min(where), max(where)) if where else None)


def fixed_threads(client, review, threads, *, head, bot_login, started):
    """The bot's own open threads from before this run that a later commit fixed.

    A thread counts as fixed when the lines it pointed at were changed or removed between the
    commit it was written on and the head, and this review raised no finding on that region
    again. Anything that cannot be read keeps the thread open.
    """
    login = bot_login.removesuffix("[bot]")
    issues = ((review or {}).get("review") or {}).get("key_issues_to_review")
    if not isinstance(issues, list):
        return []
    # A file the model could not fully review cannot show that a point was not raised again.
    coverage = (review or {}).get("coverage") if isinstance((review or {}).get("coverage"), dict) else {}
    unreviewed = {name for key in ("files_not_reviewed", "files_partially_reviewed")
                  if isinstance(coverage.get(key), list) for name in coverage[key]}
    fixed = []
    for thread in threads:
        if (thread.get("isResolved") or thread.get("author") != login
                or (thread.get("created_at") or "") >= started or not thread.get("id")):
            continue
        path, commit = thread.get("path"), thread.get("original_commit")
        if path in unreviewed:
            continue
        start, end = thread.get("original_start"), thread.get("original_end")
        if not (isinstance(path, str) and commit and isinstance(start, int) and isinstance(end, int)):
            continue
        if commit == head:
            continue
        try:
            old, new = client.file_at(path, commit), client.file_at(path, head)
        except GitHubError:
            continue
        if old is None:
            continue
        if new is None:
            changed, region = True, None  # The file is gone, so the flagged lines are too.
        else:
            changed, region = touched(old, new, min(start, end), max(start, end))
        if not changed:
            continue
        raised_again = any(
            isinstance(issue, dict) and issue.get("relevant_file") == path
            and (region is None or (_issue_lines(issue) or (0, 0))[0] <= region[1] + 3
                 and (_issue_lines(issue) or (0, 0))[1] >= region[0] - 3)
            for issue in issues)
        if not raised_again:
            fixed.append(thread)
    return fixed


def resolve_fixed_threads(client, review, *, head, bot_login, started, report=print):
    """Resolve the bot's threads that a later commit fixed. Returns how many it resolved."""
    try:
        threads = client.review_threads()
    except GitHubError as error:
        report(f"::warning::Fixed threads could not be checked: {error}")
        return 0
    count = 0
    for thread in fixed_threads(client, review, threads, head=head, bot_login=bot_login, started=started):
        try:
            client.resolve_thread(thread["id"], (
                f"Resolved by the bot: the lines this finding pointed at changed in `{head[:7]}`, "
                "and the review of that commit did not raise it again."))
            count += 1
        except GitHubError as error:
            report(f"::warning::A fixed thread could not be resolved: {error}")
    return count


def open_earlier_findings(client, bot_login, started):
    """Threads the bot opened before this run that nobody resolved: answered findings close."""
    login = bot_login.removesuffix("[bot]")
    return [thread for thread in client.review_threads()
            if not thread.get("isResolved")
            and ((thread.get("author") or "") == login)
            and (thread.get("created_at") or "") < started]


def converge_state(client, review, *, previous_head, head, bot_login, started):
    """For a later review: the findings that block, and the bot's open earlier threads.

    Raises GitHubError when either cannot be read; the caller then blocks approval.
    """
    return {"blocking": blocking_findings(client, review, previous_head, head),
            "earlier": open_earlier_findings(client, bot_login, started)}


def decision_blockers(client, review, excluded_patterns=(), *, converge=None):
    """Every reason that prevents approval of this review, including paths a person approves.

    converge, for a later review, holds the blocking findings and open earlier threads, or an
    error that blocks approval.
    """
    if converge and converge.get("error"):
        return [f"earlier findings could not be checked ({converge['error']})"]
    blocking = converge.get("blocking") if converge else None
    blockers = approval_blockers(review, blocking)
    if converge and converge.get("earlier"):
        blockers.append(f"{len(converge['earlier'])} earlier finding(s) of the bot are not resolved")
    if not blockers and excluded_patterns:
        files = client.list_files()
        expected = client.changed_file_count()
        if len(files) < expected:
            # GitHub lists at most 3000 files; an unlisted file could match a protected path.
            blockers = [f"only {len(files)} of {expected} changed files could be listed"]
        else:
            blockers = path_blockers(files, excluded_patterns)
    return blockers


def sync_approval(client, *, bot_login, head_sha_before, head_sha_after, review,
                  excluded_patterns=(), approve=True, request_changes=False,
                  report=lambda blocker: None, note="", converge=None):
    """Bring the bot's own review state in line with the latest review of an unchanged head.

    Approves a clean review (when approve is set), requests changes when the model says
    changes are required (when request_changes is set), and withdraws the bot's earlier
    verdicts that no longer hold. report() receives each reason that prevents approval.
    """
    if not head_sha_before or head_sha_before != head_sha_after:
        return "head-changed"
    bot_reviews = [record for record in client.list_reviews()
                   if (record.get("user") or {}).get("login") == bot_login]
    # GitHub lists reviews oldest first; the bot's current verdict is its latest active one.
    verdicts = [record for record in bot_reviews
                if record.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
    approvals = [record for record in verdicts if record["state"] == "APPROVED"]
    change_requests = [record for record in verdicts if record["state"] == "CHANGES_REQUESTED"]
    latest = verdicts[-1] if verdicts else None

    def is_current(state):
        return (latest is not None and latest.get("state") == state
                and latest.get("commit_id") == head_sha_after)

    blockers = decision_blockers(client, review, excluded_patterns, converge=converge)
    for blocker in blockers:
        report(blocker)
    short_sha = head_sha_after[:7]

    def withdraw(records, message):
        for record in records:
            client.dismiss(record["id"], message)

    if not blockers:
        if not approve:
            withdraw(change_requests, f"Automated DeepSeek review found no blocking concerns in {short_sha}.")
            return "no-concerns"
        if is_current("APPROVED"):
            return "already-approved"
        client.approve(head_sha_after,
                       f"Automated DeepSeek review found no blocking concerns in {short_sha}.{note}")
        # Withdraw explicitly rather than rely on the approval superseding the request.
        withdraw(change_requests, f"Automated DeepSeek review found no blocking concerns in {short_sha}.")
        return "approved"

    reasons = "; ".join(blockers)
    withdraw(approvals, f"Automated DeepSeek review of {short_sha} did not pass: {reasons}.")
    recommendation = _recommendation(review)
    # A later review that converges asks for changes only for code it had not seen before.
    if converge and not any("lines changed since the last review" in b for b in blockers):
        recommendation = recommendation if recommendation != "changes_required" else "needs_review"
    if request_changes and recommendation == "changes_required":
        if is_current("CHANGES_REQUESTED"):
            return "already-changes-requested"
        client.request_changes(head_sha_after,
                               f"Automated DeepSeek review recommends changes in {short_sha}: {reasons}. "
                               "See the review comments for details.")
        return "changes-requested"
    if recommendation not in (None, "changes_required"):
        # A completed review with a softer verdict. A failed review, or one that still requires
        # changes while request_changes is off, keeps the existing request.
        withdraw(change_requests, f"Automated DeepSeek review of {short_sha} no longer requires changes.")
    return "not-approved"


# --- GitHub API and command-line entry points -------------------------------

_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHubClient:
    """The few REST calls the approval step needs, for one pull request."""

    def __init__(self, api_url, token, repository, number):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.repository, self.number = repository, number
        self.retry_delay = float(os.environ.get("REVIEW_API_RETRY_DELAY", "1"))
        self.repo_path = f"/repos/{repository}"
        # GitHub Enterprise serves REST at /api/v3 and GraphQL at /api/graphql.
        root = self.api_url[: -len("/v3")] if self.api_url.endswith("/api/v3") else self.api_url
        self.graphql_url = f"{root}/graphql"
        self.pull_path = f"/repos/{repository}/pulls/{number}"
        self.issue_path = f"/repos/{repository}/issues/{number}"
        self.comments_path = f"/repos/{repository}/issues/comments"

    # GitHub answers these when it is briefly unavailable or rate limits; a retry usually works.
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    attempts = 3

    def _request(self, method, url, payload=None):
        """One API call. A call that is safe to repeat is tried again after a temporary failure;
        a POST that creates something, such as a comment or a review, never is."""
        if url.startswith("/"):
            url = self.api_url + url
        data = json.dumps(payload).encode() if payload is not None else None
        query = (payload or {}).get("query", "") if isinstance(payload, dict) else ""
        repeatable = method in ("GET", "PUT", "PATCH", "DELETE") or (
            method == "POST" and url == self.graphql_url and not query.lstrip().startswith("mutation"))
        for attempt in range(1, (self.attempts if repeatable else 1) + 1):
            try:
                return self._send(method, url, data)
            except GitHubError as error:
                if attempt == self.attempts or not repeatable or error.status not in self.RETRY_STATUSES:
                    raise
            except OSError:
                if attempt == self.attempts or not repeatable:
                    raise
            time.sleep(self.retry_delay * attempt)

    def _send(self, method, url, data):
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "deepseek-review-action",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return ((json.loads(body) if body else None), response.headers.get("Link", ""),
                        response.status)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise GitHubError(f"{method} {url} failed with HTTP {error.code}: {detail}",
                              status=error.code) from error

    def is_org_member(self, org, login):
        try:
            _, _, status = self._request("GET", f"/orgs/{org}/members/{login}")
        except GitHubError as error:
            if error.status == 404:
                return False
            if error.status == 403:
                raise GitHubError(
                    "members-only cannot check organization membership: grant the reviewer GitHub App "
                    "the organization permission Members: Read-only and accept it in the "
                    f"installation settings. ({error})", status=403) from error
            raise
        # Only 204 means "is a member"; anything else, such as a listing, does not.
        return status == 204

    def head_sha(self):
        pull, _, _ = self._request("GET", self.pull_path)
        return pull["head"]["sha"]

    def changed_file_count(self):
        pull, _, _ = self._request("GET", self.pull_path)
        return pull["changed_files"]

    def _paginate(self, url):
        items = []
        while url:
            page, link, _ = self._request("GET", url)
            items.extend(page)
            match = _NEXT_LINK_RE.search(link)
            url = match.group(1) if match else None
        return items

    def list_reviews(self):
        return self._paginate(f"{self.pull_path}/reviews?per_page=100")

    def list_files(self):
        return self._paginate(f"{self.pull_path}/files?per_page=100")

    def approve(self, commit_sha, body):
        self._request("POST", f"{self.pull_path}/reviews",
                      {"commit_id": commit_sha, "event": "APPROVE", "body": body})

    def request_changes(self, commit_sha, body):
        self._request("POST", f"{self.pull_path}/reviews",
                      {"commit_id": commit_sha, "event": "REQUEST_CHANGES", "body": body})

    def file_at(self, path, ref):
        """A file's text at a commit, or None when it does not exist there."""
        quoted = urllib.parse.quote(path)
        try:
            item, _, _ = self._request("GET", f"{self.repo_path}/contents/{quoted}?ref={ref}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(item, dict) or item.get("encoding") != "base64":
            raise GitHubError(f"{path} at {ref[:7]} cannot be read as one file")
        return base64.b64decode(item.get("content") or "").decode("utf-8", "replace")

    def review_threads(self):
        """Every review thread: who opened it, when, and whether it is resolved."""
        query = """query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) { pullRequest(number: $number) {
            reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor }
              nodes { id isResolved path line startLine originalLine originalStartLine
                comments(first: 1) { nodes { author { login } createdAt url originalCommit { oid } } } } } } } }"""
        owner, name = self.repository.split("/", 1)
        threads, after = [], None
        while True:
            data, _, _ = self._request("POST", self.graphql_url, {"query": query, "variables": {
                "owner": owner, "name": name, "number": int(self.number), "after": after}})
            try:
                if data.get("errors"):
                    raise KeyError("errors")
                page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes, more = list(page["nodes"]), bool(page["pageInfo"]["hasNextPage"])
            except (AttributeError, KeyError, TypeError) as error:
                # Fail closed: unknown thread states block approval through the caller.
                raise GitHubError(f"review threads could not be read: {error}") from error
            for node in nodes:
                first = (node.get("comments") or {}).get("nodes") or [{}]
                comment = first[0] or {}
                threads.append({"id": node.get("id"), "isResolved": node.get("isResolved"),
                                "author": (comment.get("author") or {}).get("login"),
                                "created_at": comment.get("createdAt"),
                                "path": node.get("path"),
                                "original_start": node.get("originalStartLine") or node.get("originalLine"),
                                "original_end": node.get("originalLine"),
                                "original_commit": (comment.get("originalCommit") or {}).get("oid"),
                                "url": comment.get("url")})
            if not more:
                return threads
            after = page["pageInfo"]["endCursor"]

    def resolve_thread(self, thread_id, body):
        """Reply in a review thread, then resolve it."""
        for query, variables in (
            ("mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply(input: "
             "{pullRequestReviewThreadId: $id, body: $body}) { comment { id } } }",
             {"id": thread_id, "body": body}),
            ("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
             "{ thread { isResolved } } }", {"id": thread_id})):
            data, _, _ = self._request("POST", self.graphql_url, {"query": query, "variables": variables})
            if not isinstance(data, dict) or data.get("errors"):
                raise GitHubError(f"the review thread could not be resolved: {(data or {}).get('errors')}")

    def list_comments(self):
        return self._paginate(f"{self.issue_path}/comments?per_page=100")

    def create_comment(self, body):
        self._request("POST", f"{self.issue_path}/comments", {"body": body})

    def update_comment(self, comment_id, body):
        self._request("PATCH", f"{self.comments_path}/{comment_id}", {"body": body})

    def remove_label(self, name):
        try:
            self._request("DELETE", f"{self.issue_path}/labels/{urllib.parse.quote(name, safe='')}")
        except GitHubError as error:
            if error.status != 404:  # Already removed, for example by a person.
                raise

    def dismiss(self, review_id, message):
        self._request("PUT", f"{self.pull_path}/reviews/{review_id}/dismissals",
                      {"message": message, "event": "DISMISS"})


def _set_outputs(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _client_from_env():
    return GitHubClient(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                        os.environ["GH_TOKEN"], os.environ["GITHUB_REPOSITORY"],
                        os.environ["PR_NUMBER"])


def _load_review(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        print(f"Review output {path} is unavailable: {error}")
        return None


def _split_list(text):
    return [item.strip() for item in re.split(r"[,\n]", text) if item.strip()]


def _validate_ignore_globs(text):
    if not text.strip():
        return
    try:
        globs = json.loads(text)
    except ValueError as error:
        raise SystemExit(f"ignore-globs must be a JSON array of strings: {error}") from error
    if not isinstance(globs, list) or not all(isinstance(glob, str) for glob in globs):
        raise SystemExit("ignore-globs must be a JSON array of strings, for example '[\"*.meta\"]'.")


def _positive_int(name, text):
    if not re.fullmatch(r"[0-9]+", text or "") or int(text) <= 0:
        raise SystemExit(f"{name} must be a positive whole number, not {text!r}.")
    return int(text)


def _true(name, default):
    value = os.environ.get(name, default)
    if value not in ("true", "false"):
        raise SystemExit(f"{name.lower().replace('_', '-')} must be \"true\" or \"false\", not {value!r}.")
    return value == "true"


def command_gate():
    _validate_ignore_globs(os.environ.get("IGNORE_GLOBS", ""))
    trigger = os.environ.get("REVIEW_ON", "push")
    if trigger not in REVIEW_TRIGGERS:
        raise SystemExit(f"review-on must be one of {', '.join(REVIEW_TRIGGERS)}, not {trigger!r}.")
    label = os.environ.get("REVIEW_LABEL", "review").strip()
    if not label:
        raise SystemExit("review-label must not be empty.")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise SystemExit("This action must run on a pull_request or pull_request_target event.")
    wanted, stale, requested, why = review_wanted(event, trigger=trigger, label=label)
    if not wanted and not stale:
        print(f"Skipping review: {why}")
        _set_outputs(run="false")
        return
    allowed_authors = _split_list(os.environ.get("ALLOWED_AUTHORS", ""))
    is_member = None
    if os.environ.get("MEMBERS_ONLY", "false") == "true":
        owner = (event.get("repository") or {}).get("owner") or {}
        if owner.get("type") != "Organization":
            raise SystemExit("members-only needs a repository owned by an organization.")
        token = os.environ.get("GH_TOKEN", "")
        client = GitHubClient(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                              token, os.environ["GITHUB_REPOSITORY"], pull_request["number"])

        def is_member(login):
            if not token:
                raise SystemExit(
                    "members-only has no reviewer token: if the token step failed, grant the reviewer "
                    "GitHub App the organization permission Members: Read-only and accept it in the "
                    "installation settings.")
            return client.is_org_member(owner["login"], login)
    # A pull request the bot skips is never touched, also when its approval would be stale.
    allowed, reason = author_allowed(pull_request, allowed_authors, is_member=is_member)
    if allowed and stale:
        print(f"Skipping review: {why}")
        _set_outputs(run="false", stale="true", **{"pr-number": pull_request["number"]})
        return
    print(f"{'Reviewing' if allowed else 'Skipping review'}: {reason}; {why}")
    if allowed:
        extra = {"label-requested": "true"} if requested else {}
        _set_outputs(run="true", **{"pr-number": pull_request["number"],
                                    "pr-url": pull_request["html_url"]}, **extra)
    else:
        _set_outputs(run="false")


def command_take_label():
    """Remove the review label, so that adding it again asks for another review."""
    label = os.environ.get("REVIEW_LABEL", "review").strip()
    try:
        _client_from_env().remove_label(label)
    except (GitHubError, OSError) as error:
        # Only a convenience: the review goes on, and a person can remove the label.
        print(f"::warning::The bot could not remove the {label!r} label, so remove it before adding "
              f"it again ({error}).")


def command_data_files():
    """The ignore list for pr-agent: the user's globs plus the data files of this pull request."""
    text = os.environ.get("IGNORE_GLOBS", "")
    _validate_ignore_globs(text)
    user_globs = json.loads(text) if text.strip() else []
    skipped = []
    if _true("SKIP_DATA_FILES", "true"):
        max_lines = _positive_int("data-file-lines", os.environ.get("DATA_FILE_LINES", "1000"))
        protected = _split_list(os.environ.get("APPROVAL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS))
        try:
            listed = _client_from_env().list_files()
        except (GitHubError, OSError) as error:
            # This step only helps the review; a GitHub hiccup must not stop it.
            print(f"::warning::Data files could not be found, so every file is reviewed: could not list "
                  f"the changed files ({error}).")
            listed = []
        found = data_files(listed, max_lines=max_lines, protected=protected)
        globs, skipped = ignore_list(user_globs, found)
        if len(skipped) < len(found):
            print(f"{len(found) - len(skipped)} more data file(s) are reviewed: the ignore list is full.")
    else:
        globs = user_globs
    for path in skipped:
        print(f"Leaving out of the review as data: {path}")
    out = os.path.join(os.environ["RUNNER_TEMP"], "deepseek-review")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "data-files.json"), "w", encoding="utf-8") as handle:
        json.dump(skipped, handle)
    _set_outputs(**{"ignore-globs": json.dumps(globs) if globs else ""})


def command_stale():
    """After new commits in label mode: withdraw the bot's approval of older commits, and say so."""
    approve, comment = _true("APPROVE", "true"), _true("RESULT_COMMENT", "true")
    client = _client_from_env()
    bot_login = os.environ["BOT_LOGIN"]
    label = os.environ.get("REVIEW_LABEL", "review").strip()
    head = client.head_sha()
    mine = [record for record in client.list_reviews()
            if (record.get("user") or {}).get("login") == bot_login]
    # Events can arrive late: the current head may already be reviewed, whatever the verdict.
    if any(record.get("commit_id") == head for record in mine):
        print("The current head is already reviewed; nothing is stale.")
        return
    approvals = [record for record in mine if record.get("state") == "APPROVED"]
    if approve:
        failures = []
        for record in approvals:
            try:
                client.dismiss(record["id"], "New commits are not reviewed yet; "
                                             f"add the {label!r} label to review them.")
            except GitHubError as error:
                if error.status in (404, 422):  # Already dismissed, for example by a person.
                    continue
                failures.append(error)  # Try the others first.
        if failures:
            raise failures[0]
    if comment:
        post_result(client, bot_login, stale_comment(label, previous_head(client, bot_login)))


def command_head_sha():
    # Threads the bot opens from now on belong to this run; older ones are earlier findings.
    started = datetime.datetime.now(datetime.timezone.utc)
    _set_outputs(sha=_client_from_env().head_sha(), started=started.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _skipped_data_files():
    path = os.path.join(os.environ.get("RUNNER_TEMP", ""), "deepseek-review", "data-files.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return []
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def command_approve():
    # Every flag is checked before any change, so a bad value changes nothing.
    approve = _true("APPROVE", "true")
    request_changes = _true("REQUEST_CHANGES", "false")
    comment = _true("RESULT_COMMENT", "true")
    converging = _true("CONVERGE", "true")
    resolving = _true("RESOLVE_FIXED_THREADS", "true")
    client = _client_from_env()
    review = _load_review(os.environ["REVIEW_JSON"])
    excluded = _split_list(os.environ.get("APPROVAL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS))
    skipped = _skipped_data_files()
    head_sha_before = os.environ.get("HEAD_SHA_BEFORE", "")
    head_sha_after = client.head_sha()
    blockers = []
    note = (f" {len(skipped)} data file(s) were left out of the review for a person to check."
            if skipped else "")
    bot_login = os.environ["BOT_LOGIN"]
    started = os.environ.get("RUN_STARTED", "")
    resolved = 0
    if resolving and isinstance(review, dict) and started and head_sha_before == head_sha_after:
        # First, so a thread a commit fixed no longer counts as an open earlier finding.
        resolved = resolve_fixed_threads(client, review, head=head_sha_after, bot_login=bot_login,
                                         started=started)
        if resolved:
            print(f"Resolved {resolved} thread(s) of the bot that a later commit fixed.")
    earlier_head = previous_head(client, bot_login) if (converging or comment) else ""
    # A later review blocks only on what it had not seen; the first review stays strict.
    converge = None
    if converging and earlier_head and isinstance(review, dict) and head_sha_before == head_sha_after:
        try:
            converge = converge_state(client, review, previous_head=earlier_head, head=head_sha_after,
                                      bot_login=bot_login, started=started)
        except GitHubError as error:
            converge = {"error": str(error)}
    if approve or request_changes:
        result = sync_approval(client, bot_login=bot_login,
                               head_sha_before=head_sha_before, head_sha_after=head_sha_after,
                               review=review, excluded_patterns=excluded, approve=approve,
                               request_changes=request_changes, note=note, converge=converge,
                               report=lambda blocker: (blockers.append(blocker),
                                                       print(f"Not approving: {blocker}")))
    else:
        result = "" if head_sha_before == head_sha_after else "head-changed"
        if not result:
            blockers = decision_blockers(client, review, excluded, converge=converge)
    print(f"Approval result: {result}")
    _set_outputs(result=result)
    if comment:
        blocking = (converge or {}).get("blocking")
        decided = result != "head-changed" and isinstance(review, dict)
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        post_result(client, bot_login, result_comment(
            head_sha=head_sha_after, result=result, blockers=blockers, review=review,
            skipped=skipped, blocking=blocking, resolved=resolved,
            earlier=(converge or {}).get("earlier") or [],
            repo_url=f"{server}/{repository}" if repository else "",
            reviewed_head=head_sha_after if decided else earlier_head))


COMMANDS = {"gate": command_gate, "head-sha": command_head_sha, "approve": command_approve,
            "data-files": command_data_files, "take-label": command_take_label,
            "stale": command_stale}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        raise SystemExit(f"usage: {argv[0]} {{{','.join(COMMANDS)}}}")
    try:
        COMMANDS[argv[1]]()
    except GitHubError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv)
