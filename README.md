# DeepSeek PR Review

[English](README.md) | [Русский](README.ru.md)

A GitHub Action that reviews pull requests with [pr-agent](https://github.com/qodo-ai/pr-agent) and DeepSeek. It posts the review as your own GitHub App bot, so the comments are clearly from the reviewer and not from `github-actions[bot]`. It can also approve clean reviews.

## What it does

1. Skips draft pull requests and authors you do not trust (see [Who gets reviewed](#who-gets-reviewed)).
2. Creates a short-lived token for the reviewer GitHub App. The token is limited to the current repository.
3. Runs pr-agent in a container. It reads the diff through the GitHub API and posts a summary comment and inline findings. Pull request code is never checked out or executed.
4. Approves the pull request when all of these hold:
   - the merge recommendation is `no_concerns_found`;
   - no key issues and no security concerns were reported;
   - every changed file was fully reviewed;
   - the head commit did not change while the review ran.

   The approval is pinned to the reviewed commit. In any other case, the bot withdraws (dismisses) its own earlier approvals. A failed review also withdraws them. Reviews by people are never touched.

The approval is a signal, not a guarantee. A language model can miss defects, and the text of a pull request can try to manipulate it. Keep human review for anything that matters.

## Setup

### Once per account: the reviewer GitHub App

1. Go to **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Give it a name and logo. Disable **Webhook → Active**.
3. Under **Repository permissions**, set **Contents: Read-only**, **Issues: Read and write** and **Pull requests: Read and write**.
4. Create the app. Note its **App ID** and generate a **private key**.
5. To use the app in an organization, open **Advanced → Make public** and install the app on the organization. A public app does not expose your private key.

### Per repository

1. **Install App** → add the repository to the installation.
2. Add these Actions settings under **Settings → Secrets and variables → Actions**:
   - secret `DEEPSEEK_API_KEY`;
   - secret `REVIEW_APP_PRIVATE_KEY`: the whole PEM file;
   - variable `REVIEW_APP_ID`.

   In an organization on a paid plan, or for public repositories, you can define them once at the organization level. On GitHub Free, organization secrets are not available to private repositories.
3. Add `.github/workflows/deepseek-review.yml`:

```yaml
name: DeepSeek PR Review

on:
  pull_request_target:
    types: [opened, reopened, ready_for_review, synchronize]

permissions: {}

concurrency:
  group: deepseek-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: devnullexile/deepseek-review@v1
        with:
          deepseek-api-key: ${{ secrets.DEEPSEEK_API_KEY }}
          app-id: ${{ vars.REVIEW_APP_ID }}
          app-private-key: ${{ secrets.REVIEW_APP_PRIVATE_KEY }}
          # Optional:
          # allowed-authors: alice, bob
          # approve: "false"
          # extra-instructions: |
          #   Check that every user-facing string exists in both locales.
```

`pull_request_target` runs the workflow file from the base branch with access to secrets, including for pull requests from forks. That is safe here only because no pull request code is executed. Do not add a step that checks out and runs the pull request head.

## Runner requirements

The action needs a Linux runner with Docker and Python 3. GitHub-hosted `ubuntu-latest` runners have both.

## Who gets reviewed

By default, the action reviews pull requests whose authors are repository owners, organization members or collaborators. Each review costs DeepSeek tokens, so strangers cannot spend your key. To list exact logins instead, set `allowed-authors`. GitHub may report private organization members as `CONTRIBUTOR`; list them explicitly if that happens.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `deepseek-api-key` | required | DeepSeek API key |
| `app-id` or `client-id` | one required | Reviewer GitHub App identifier |
| `app-private-key` | required | Reviewer GitHub App private key |
| `allowed-authors` | empty | Logins to review; empty means owners, members and collaborators |
| `approve` | `true` | Approve clean reviews and withdraw stale bot approvals |
| `extra-instructions` | empty | Project rules appended to the default review policy |
| `ignore-globs` | empty | JSON array of globs to skip, e.g. `'["*.meta", "*.unity"]'`; skipped files are not reviewed and do not block approval |
| `model` | `deepseek/deepseek-flash` | LiteLLM model name; this is DeepSeek-V4.1-Flash |
| `max-model-tokens` | `512000` | Token window per call; the prompt gets what remains after `max-output-tokens` (128K by default) |
| `max-output-tokens` | `384000` | Output token limit per call, including the model's thinking; lower it, together with `max-model-tokens`, for models with a smaller limit |
| `response-language` | `en-US` | Review language |
| `max-findings` | `5` | Maximum number of findings |
| `pr-agent-image` | `pragent/pr-agent:0.49.0` | pr-agent image; the approval step relies on its `--json-output` format |

Outputs: `reviewed` (`true` when reviewed) and `approval` (`approved`, `already-approved`, `not-approved`, `head-changed`).

## Versions

Pin `@v1` for compatible updates, or a full commit SHA for an exact version. A change to the pr-agent image, or to how approval is decided, comes in a new major version.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The `action.yml` contract tests need PyYAML and are skipped without it.

The approval logic lives in `scripts/review_gate.py` and uses only the Python standard library.
