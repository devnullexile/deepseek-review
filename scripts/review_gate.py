"""Gate and approval logic for the DeepSeek pull request review action."""

import fnmatch
import json
import os
import posixpath
import re
import sys
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
REVIEW_TRIGGERS = ("push", "label")


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


def approval_blockers(output):
    """Return reasons that prevent approval; an empty list means approve.

    The check fails closed: anything missing or unexpected blocks approval.
    """
    if not isinstance(output, dict) or not isinstance(output.get("review"), dict):
        return ["review output is missing or malformed"]
    review = output["review"]
    blockers = []

    recommendation = review.get("merge_recommendation")
    if recommendation != "no_concerns_found":
        blockers.append(f"merge recommendation is {recommendation!r}")

    issues = review.get("key_issues_to_review")
    if not isinstance(issues, list):
        blockers.append("key issues are missing")
    elif issues:
        blockers.append(f"{len(issues)} key issue(s) reported")

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


def result_comment(*, head_sha, result, blockers, review, skipped):
    """The bot's own result comment: what it decided, and every reason, in one place."""
    title = RESULT_TITLES.get(result, "Not approved")
    lines = [RESULT_MARKER, f"### DeepSeek review: {title} (`{head_sha[:7]}`)", ""]
    if result == "head-changed":
        lines.append("New commits arrived during this review, so it decides nothing. "
                     "The next review of the latest commit will.")
    elif blockers:
        lines.append("What stands in the way of approval:")
        lines += [f"- {_text(blocker, 300)}" for blocker in blockers]
    else:
        lines.append("The review found nothing that blocks approval.")
    # A readable review can still have any shape: nothing here may fail on it.
    output = review if isinstance(review, dict) else {}
    body = output.get("review") if isinstance(output.get("review"), dict) else {}
    issues = body.get("key_issues_to_review")
    if isinstance(issues, list) and issues:
        lines += ["", "Findings of this review:"]
        for issue in issues[:10]:
            if not isinstance(issue, dict):
                continue
            where = str(issue.get("relevant_file") or "")
            if issue.get("start_line"):
                where += f":{issue.get('start_line')}"
            lines.append(f"- {_code(where, 200)} **{_text(issue.get('issue_header'), 80)}**: "
                         f"{_text(issue.get('issue_content'), 300)}")
    coverage = output.get("coverage") if isinstance(output.get("coverage"), dict) else {}
    unreviewed = [name for key in ("files_not_reviewed", "files_partially_reviewed")
                  if isinstance(coverage.get(key), list)
                  for name in coverage[key] if isinstance(name, str)]
    if unreviewed:
        lines += ["", f"The model could not review in full: {_listed(unreviewed)}."]
    if skipped:
        lines += ["", f"Left out of the review as data files, for a person to check: {_listed(skipped)}."]
    lines += ["", "_This comment is updated on every review. The review comment has the details._"]
    return "\n".join(lines)


def stale_comment(label):
    return "\n".join([
        RESULT_MARKER, "### DeepSeek review: Not reviewed yet", "",
        f"New commits arrived after the last review. Add the `{label}` label to review them.",
        "", "_This comment is updated on every review._"])


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


def decision_blockers(client, review, excluded_patterns=()):
    """Every reason that prevents approval of this review, including paths a person approves."""
    blockers = approval_blockers(review)
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
                  report=lambda blocker: None, note=""):
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

    blockers = decision_blockers(client, review, excluded_patterns)
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
        self.pull_path = f"/repos/{repository}/pulls/{number}"
        self.issue_path = f"/repos/{repository}/issues/{number}"
        self.comments_path = f"/repos/{repository}/issues/comments"

    def _request(self, method, url, payload=None):
        if url.startswith("/"):
            url = self.api_url + url
        data = json.dumps(payload).encode() if payload is not None else None
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
    _client_from_env().remove_label(os.environ.get("REVIEW_LABEL", "review").strip())


def command_data_files():
    """The ignore list for pr-agent: the user's globs plus the data files of this pull request."""
    text = os.environ.get("IGNORE_GLOBS", "")
    _validate_ignore_globs(text)
    user_globs = json.loads(text) if text.strip() else []
    skipped = []
    if _true("SKIP_DATA_FILES", "true"):
        max_lines = _positive_int("data-file-lines", os.environ.get("DATA_FILE_LINES", "1000"))
        protected = _split_list(os.environ.get("APPROVAL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS))
        found = data_files(_client_from_env().list_files(), max_lines=max_lines, protected=protected)
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
    client = _client_from_env()
    bot_login = os.environ["BOT_LOGIN"]
    label = os.environ.get("REVIEW_LABEL", "review").strip()
    if _true("APPROVE", "true"):
        for record in client.list_reviews():
            if ((record.get("user") or {}).get("login") == bot_login
                    and record.get("state") == "APPROVED"):
                client.dismiss(record["id"], "New commits are not reviewed yet; "
                                             f"add the {label!r} label to review them.")
    if _true("RESULT_COMMENT", "true"):
        post_result(client, bot_login, stale_comment(label))


def command_head_sha():
    _set_outputs(sha=_client_from_env().head_sha())


def _skipped_data_files():
    path = os.path.join(os.environ.get("RUNNER_TEMP", ""), "deepseek-review", "data-files.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return []
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def command_approve():
    client = _client_from_env()
    review = _load_review(os.environ["REVIEW_JSON"])
    excluded = _split_list(os.environ.get("APPROVAL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS))
    approve = _true("APPROVE", "true")
    request_changes = _true("REQUEST_CHANGES", "false")
    skipped = _skipped_data_files()
    head_sha_before = os.environ.get("HEAD_SHA_BEFORE", "")
    head_sha_after = client.head_sha()
    blockers = []
    note = (f" {len(skipped)} data file(s) were left out of the review for a person to check."
            if skipped else "")
    if approve or request_changes:
        result = sync_approval(client, bot_login=os.environ["BOT_LOGIN"],
                               head_sha_before=head_sha_before, head_sha_after=head_sha_after,
                               review=review, excluded_patterns=excluded, approve=approve,
                               request_changes=request_changes, note=note,
                               report=lambda blocker: (blockers.append(blocker),
                                                       print(f"Not approving: {blocker}")))
    else:
        result = "" if head_sha_before == head_sha_after else "head-changed"
        if not result:
            blockers = decision_blockers(client, review, excluded)
    print(f"Approval result: {result}")
    _set_outputs(result=result)
    if _true("RESULT_COMMENT", "true"):
        post_result(client, os.environ["BOT_LOGIN"], result_comment(
            head_sha=head_sha_after, result=result, blockers=blockers, review=review,
            skipped=skipped))


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
