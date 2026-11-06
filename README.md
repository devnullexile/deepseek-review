# DeepSeek PR Review

[English](README.md) | [Русский](README.ru.md)

A GitHub Action that reviews pull requests with [pr-agent](https://github.com/qodo-ai/pr-agent) and DeepSeek. It posts the review as your own GitHub App bot, so the comments are clearly from the reviewer and not from `github-actions[bot]`. It can also approve clean reviews.

## What it does

1. Skips draft pull requests and authors you do not trust (see [Who gets reviewed](#who-gets-reviewed)).
2. Creates a short-lived token for the reviewer GitHub App. The token is limited to the current repository.
3. Runs pr-agent in a container. It reads the diff through the GitHub API and posts a summary comment and inline findings. Pull request code is never checked out or executed. Data files are left out of the review (see [Data files](#data-files)), and the model sees its earlier findings and the replies in their resolved threads, so it does not raise them again in other words. If an attempt fails or writes no readable review, for example after the DeepSeek API drops a connection, the review runs again, up to `review-attempts` times. If every attempt fails, the step fails.
4. Approves the pull request when all of these hold:
   - the merge recommendation is `no_concerns_found`;
   - no key issues and no security concerns were reported;
   - every changed file that was not left out as data was fully reviewed;
   - no changed file matches `approval-excluded-paths` (by default `.github/*`, so a person approves CI changes);
   - the head commit did not change while the review ran.

   After the first review of a pull request, a later review converges (`converge`): only findings on lines added or changed since the bot's last review block, and so do the bot's own earlier threads that nobody resolved. A point raised again on code the bot already reviewed, after a person answered and resolved it, no longer blocks, and the model's merge recommendation is then advisory. Security concerns, files not fully reviewed and protected paths still block.

   The approval is pinned to the reviewed commit. In any other case, the bot withdraws (dismisses) its own earlier approvals. A failed review also withdraws them. Reviews by people are never touched.
5. Optionally (`request-changes: "true"`), requests changes when the model's verdict is `changes_required`. A later approval by the bot supersedes the request. A later completed review with a softer verdict withdraws it. A failed review keeps it, so a merge that the request blocks stays blocked.

6. Resolves its own threads that a later commit fixed (`resolve-fixed-threads`): the lines a thread pointed at were changed or removed, and the review of the new commit raised no finding there again. It replies in the thread first, saying which commit changed the lines.
7. Keeps one result comment up to date with what the bot decided on the latest commit and a checklist of what to change before it approves:
   - each blocking finding, with a link to its file and lines;
   - each earlier thread that is still open, with a link to it;
   - any other blocker, such as security concerns or files not fully reviewed.

   Below the checklist it lists the findings that do not block, the data files a person should check, and the threads the bot resolved. It is updated on each review, not repeated.

The approval is a signal, not a guarantee. A language model can miss defects, and the text of a pull request can try to manipulate it. Keep human review for anything that matters.

## Setup

### Once per account: the reviewer GitHub App

1. Go to **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Give it a name and logo. Disable **Webhook → Active**.
3. Under **Repository permissions**, set **Contents: Read-only**, **Issues: Read and write** and **Pull requests: Read and write**.
4. Create the app. Note its **Client ID** (it starts with `Iv`) and generate a **private key**.
5. To use the app in an organization, open **Advanced → Make public** and install the app on the organization. A public app does not expose your private key.

### Per repository

1. **Install App** → add the repository to the installation.
2. Add these Actions settings under **Settings → Secrets and variables → Actions**:
   - secret `DEEPSEEK_API_KEY`;
   - secret `REVIEW_APP_PRIVATE_KEY`: the whole PEM file;
   - variable `REVIEW_CLIENT_ID`.

   In an organization on a paid plan, or for public repositories, you can define them once at the organization level. On GitHub Free, organization secrets are not available to private repositories.
3. Add `.github/workflows/deepseek-review.yml`:

```yaml
name: DeepSeek PR Review

on:
  pull_request_target:
    types: [opened, reopened, ready_for_review, synchronize, edited]

permissions: {}

concurrency:
  group: deepseek-review-${{ github.event.pull_request.number }}${{ github.event.action == 'edited' && !github.event.changes.base && '-edit' || '' }}
  cancel-in-progress: true

jobs:
  review:
    if: github.event.action != 'edited' || github.event.changes.base
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: devnullexile/deepseek-review@v1
        with:
          deepseek-api-key: ${{ secrets.DEEPSEEK_API_KEY }}
          client-id: ${{ vars.REVIEW_CLIENT_ID }}
          app-private-key: ${{ secrets.REVIEW_APP_PRIVATE_KEY }}
          # Optional:
          # allowed-authors: alice, bob
          # approve: "false"
          # extra-instructions: |
          #   Check that every user-facing string exists in both locales.
```

The `edited` trigger, the job condition and the `-edit` concurrency group review a pull request again when its base branch changes, for example when a stacked pull request is retargeted: the diff changes without a new commit, so an earlier approval would otherwise stay. A title or body edit starts no review and never cancels a running one.

`pull_request_target` runs the workflow file from the base branch with access to secrets, including for pull requests from forks. That is safe here only because no pull request code is executed. Do not add a step that checks out and runs the pull request head.

## Data files

Lock files, JSON Lines, CSV and minified files, and JSON, YAML, XML or TOML files with more than `data-file-lines` (1000) added lines, are left out of the model review. They stay in the pull request: nothing is moved or removed. A model cannot review such files usefully, and a large one fills the token window, so the code would be left unreviewed. The result comment lists them for a person to check, and they do not block approval, like `ignore-globs`. A path in `approval-excluded-paths` is always reviewed. Turn this off with `skip-data-files: "false"`.

## Review on request

By default every new commit is reviewed (`review-on: push`). For a big pull request that goes through many rounds of fixes, set `review-on: label`. The pull request is then reviewed when it is opened, reopened, marked ready for review or retargeted, and after that only when someone adds the `review` label: on the pull request page, click **Labels** in the right column and choose `review`, or run `gh pr edit <number> --add-label review`. The bot removes the label when the review starts, so adding it again asks for another review of the latest commit. A new commit without the label withdraws the bot's approval of older commits, and the result comment says so. Create the label once under **Issues → Labels → New label**, and add `labeled` to the workflow's event types:

```yaml
on:
  pull_request_target:
    types: [opened, reopened, ready_for_review, synchronize, edited, labeled]
```

With `labeled` in the list, the `review` label also asks for a review in the default `push` mode.

## Runner requirements

The action needs a Linux runner with Docker and Python 3. GitHub-hosted `ubuntu-latest` runners have both.

## Who gets reviewed

By default, the action reviews pull requests whose authors are repository owners, organization members or collaborators. Each review costs DeepSeek tokens, so strangers cannot spend your key. To list exact logins instead, set `allowed-authors`. GitHub may report private organization members as `CONTRIBUTOR`, so the association rule can miss them.

To review only members of the organization that owns the repository, set `members-only: "true"`. Membership is then checked through the GitHub API, so private members count and outside collaborators do not. Logins in `allowed-authors` are reviewed too. This needs the organization permission **Members: Read-only** on the reviewer app: add it under the app's **Permissions & events**, then accept the new permission in the organization's installation settings.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `deepseek-api-key` | required | DeepSeek API key |
| `client-id` | required | Reviewer GitHub App Client ID; the numeric App ID also works |
| `app-id` | empty | Deprecated: the numeric App ID, used when `client-id` is empty. Use `client-id` instead |
| `app-private-key` | required | Reviewer GitHub App private key |
| `allowed-authors` | empty | Logins to review; empty means owners, members and collaborators |
| `request-changes` | `false` | Request changes on a `changes_required` verdict; useful with branch protection |
| `members-only` | `false` | Review only members of the owning organization (needs Members: Read-only) |
| `approve` | `true` | Approve clean reviews and withdraw stale bot approvals |
| `approval-excluded-paths` | `.github/*` | Globs the bot never approves; a person must. `*` also matches `/`. Empty allows any path |
| `extra-instructions` | empty | Project rules appended to the default review policy |
| `ignore-globs` | empty | JSON array of globs to skip, e.g. `'["*.meta", "*.unity"]'`; skipped files are not reviewed and do not block approval |
| `skip-data-files` | `true` | Leave [data files](#data-files) out of the review; they are listed in the result comment |
| `data-file-lines` | `1000` | Added lines above which a JSON, YAML, XML or TOML file counts as data |
| `max-previous-findings-chars` | `60000` | Room for earlier findings and the replies in their resolved threads, so they are not raised again; pr-agent's own default is 8000, and `0` turns it off |
| `result-comment` | `true` | Keep one comment with what the bot decided and why |
| `converge` | `true` | After the first review, block only on findings on code changed since the last review and on the bot's own open threads |
| `resolve-fixed-threads` | `true` | Resolve the bot's threads whose lines a later commit changed, when the new review raised nothing there again |
| `review-on` | `push` | `push` reviews every commit; `label` reviews new commits only when the review label is added (see [Review on request](#review-on-request)) |
| `review-label` | `review` | Label that asks for a review of the latest commit; the bot removes it when the review starts |
| `model` | `deepseek/deepseek-flash` | LiteLLM model name; this is DeepSeek-V4.1-Flash |
| `max-model-tokens` | `512000` | Token window per call; the prompt gets what remains after `max-output-tokens` (128K by default) |
| `max-output-tokens` | `384000` | Output token limit per call, including the model's thinking; lower it, together with `max-model-tokens`, for models with a smaller limit |
| `response-language` | `en-US` | Review language |
| `max-findings` | `5` | Maximum number of findings |
| `pr-agent-image` | `pragent/pr-agent:0.49.0` | pr-agent image; the approval step relies on its `--json-output` format |
| `review-attempts` | `2` | Attempts when a review fails or writes no readable review; `1` disables retries. Each attempt can call the model again |
| `retry-delay-seconds` | `30` | Wait before retrying a failed attempt |
| `fallback-models` | `[]` | JSON array of models pr-agent tries, in order, when the main model fails within an attempt; empty means none |

Outputs: `reviewed` (`true` when reviewed) and `approval` (`approved`, `already-approved`, `changes-requested`, `already-changes-requested`, `no-concerns`, `not-approved`, `head-changed`).

## Versions

Pin `@v1` for compatible updates, or a full commit SHA for an exact version. A change to the pr-agent image, or to how approval is decided, comes in a new major version.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The `action.yml` contract tests need PyYAML and are skipped without it.

The approval logic lives in `scripts/review_gate.py` and uses only the Python standard library.
