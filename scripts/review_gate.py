"""Gate and approval logic for the DeepSeek pull request review action."""

import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request

WRITER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
NEGATIVE_SECURITY_ANSWERS = frozenset({"no", "none", "false"})
# Used when APPROVAL_EXCLUDED_PATHS is unset; only an explicitly empty value approves any path.
DEFAULT_EXCLUDED_PATHS = ".github/*"


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


def _recommendation(output):
    """The merge recommendation of a parsed review, or None when the review is unusable."""
    if not isinstance(output, dict) or not isinstance(output.get("review"), dict):
        return None
    return output["review"].get("merge_recommendation")


def sync_approval(client, *, bot_login, head_sha_before, head_sha_after, review,
                  excluded_patterns=(), approve=True, request_changes=False,
                  report=lambda blocker: None):
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

    blockers = approval_blockers(review)
    if not blockers and excluded_patterns:
        files = client.list_files()
        expected = client.changed_file_count()
        if len(files) < expected:
            # GitHub lists at most 3000 files; an unlisted file could match a protected path.
            blockers = [f"only {len(files)} of {expected} changed files could be listed"]
        else:
            blockers = path_blockers(files, excluded_patterns)
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
        client.approve(head_sha_after, f"Automated DeepSeek review found no blocking concerns in {short_sha}.")
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
                return (json.loads(body) if body else None), response.headers.get("Link", "")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise GitHubError(f"{method} {url} failed with HTTP {error.code}: {detail}",
                              status=error.code) from error

    def is_org_member(self, org, login):
        try:
            self._request("GET", f"/orgs/{org}/members/{login}")
        except GitHubError as error:
            if error.status == 404:
                return False
            if error.status == 403:
                raise GitHubError(
                    "members-only cannot check organization membership: grant the reviewer GitHub App "
                    "the organization permission Members: Read-only and accept it in the "
                    f"installation settings. ({error})", status=403) from error
            raise
        return True

    def head_sha(self):
        pull, _ = self._request("GET", self.pull_path)
        return pull["head"]["sha"]

    def changed_file_count(self):
        pull, _ = self._request("GET", self.pull_path)
        return pull["changed_files"]

    def _paginate(self, url):
        items = []
        while url:
            page, link = self._request("GET", url)
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


def command_gate():
    _validate_ignore_globs(os.environ.get("IGNORE_GLOBS", ""))
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise SystemExit("This action must run on a pull_request or pull_request_target event.")
    allowed_authors = _split_list(os.environ.get("ALLOWED_AUTHORS", ""))
    is_member = None
    if os.environ.get("MEMBERS_ONLY", "false") == "true":
        owner = (event.get("repository") or {}).get("owner") or {}
        if owner.get("type") != "Organization":
            raise SystemExit("members-only needs a repository owned by an organization.")
        client = GitHubClient(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                              os.environ["GH_TOKEN"], os.environ["GITHUB_REPOSITORY"],
                              pull_request["number"])
        is_member = lambda login: client.is_org_member(owner["login"], login)  # noqa: E731
    allowed, reason = author_allowed(pull_request, allowed_authors, is_member=is_member)
    print(f"{'Reviewing' if allowed else 'Skipping review'}: {reason}")
    if allowed:
        _set_outputs(run="true", **{"pr-number": pull_request["number"],
                                    "pr-url": pull_request["html_url"]})
    else:
        _set_outputs(run="false")


def command_head_sha():
    _set_outputs(sha=_client_from_env().head_sha())


def command_approve():
    client = _client_from_env()
    review = _load_review(os.environ["REVIEW_JSON"])
    result = sync_approval(client, bot_login=os.environ["BOT_LOGIN"],
                           head_sha_before=os.environ.get("HEAD_SHA_BEFORE", ""),
                           head_sha_after=client.head_sha(), review=review,
                           excluded_patterns=_split_list(
                               os.environ.get("APPROVAL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS)),
                           approve=os.environ.get("APPROVE", "true") == "true",
                           request_changes=os.environ.get("REQUEST_CHANGES", "false") == "true",
                           report=lambda blocker: print(f"Not approving: {blocker}"))
    print(f"Approval result: {result}")
    _set_outputs(result=result)


COMMANDS = {"gate": command_gate, "head-sha": command_head_sha, "approve": command_approve}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        raise SystemExit(f"usage: {argv[0]} {{{','.join(COMMANDS)}}}")
    try:
        COMMANDS[argv[1]]()
    except GitHubError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main(sys.argv)
