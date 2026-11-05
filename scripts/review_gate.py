"""Gate and approval logic for the DeepSeek pull request review action."""

import json
import os
import re
import sys
import urllib.error
import urllib.request

WRITER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
NEGATIVE_SECURITY_ANSWERS = frozenset({"no", "none", "false"})


def author_allowed(pull_request, allowed_authors):
    """Return (allowed, reason) for reviewing this pull request."""
    if pull_request.get("draft"):
        return False, "pull request is a draft"
    login = (pull_request.get("user") or {}).get("login", "")
    allowed = {name.strip().lower() for name in allowed_authors if name.strip()}
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


def sync_approval(client, *, bot_login, head_sha_before, head_sha_after, review):
    """Approve a clean review of an unchanged head, or withdraw the bot's approvals."""
    if not head_sha_before or head_sha_before != head_sha_after:
        return "head-changed"
    bot_approvals = [
        record for record in client.list_reviews()
        if (record.get("user") or {}).get("login") == bot_login and record.get("state") == "APPROVED"
    ]
    blockers = approval_blockers(review)
    short_sha = head_sha_after[:7]
    if not blockers:
        if any(record.get("commit_id") == head_sha_after for record in bot_approvals):
            return "already-approved"
        client.approve(head_sha_after,
                       f"Automated DeepSeek review found no blocking concerns in {short_sha}.")
        return "approved"
    reasons = "; ".join(blockers)
    for record in bot_approvals:
        client.dismiss(record["id"], f"Automated DeepSeek review of {short_sha} did not pass: {reasons}.")
    return "not-approved"


# --- GitHub API and command-line entry points -------------------------------

_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    pass


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
            raise GitHubError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error

    def head_sha(self):
        pull, _ = self._request("GET", self.pull_path)
        return pull["head"]["sha"]

    def list_reviews(self):
        reviews, url = [], f"{self.pull_path}/reviews?per_page=100"
        while url:
            page, link = self._request("GET", url)
            reviews.extend(page)
            match = _NEXT_LINK_RE.search(link)
            url = match.group(1) if match else None
        return reviews

    def approve(self, commit_sha, body):
        self._request("POST", f"{self.pull_path}/reviews",
                      {"commit_id": commit_sha, "event": "APPROVE", "body": body})

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


def command_gate():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise SystemExit("This action must run on a pull_request or pull_request_target event.")
    allowed_authors = re.split(r"[,\n]", os.environ.get("ALLOWED_AUTHORS", ""))
    allowed, reason = author_allowed(pull_request, allowed_authors)
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
    for blocker in approval_blockers(review):
        print(f"Not approving: {blocker}")
    result = sync_approval(client, bot_login=os.environ["BOT_LOGIN"],
                           head_sha_before=os.environ.get("HEAD_SHA_BEFORE", ""),
                           head_sha_after=client.head_sha(), review=review)
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
