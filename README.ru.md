# DeepSeek PR Review

[English](README.md) | [Русский](README.ru.md)

GitHub Action, который делает ревью pull request с помощью [pr-agent](https://github.com/qodo-ai/pr-agent) и DeepSeek. Ревью публикуется от имени вашего бота (GitHub App), поэтому сразу видно, что комментарии оставил ревьюер, а не `github-actions[bot]`. Ещё бот может ставить approve, если ревью чистое.

## Что делает

1. Пропускает черновики (draft) и PR от авторов, которым вы не доверяете (см. [Чьи PR проверяются](#чьи-pr-проверяются)).
2. Получает для бота короткоживущий токен. Токен действует только в текущем репозитории.
3. Запускает pr-agent в контейнере. Он читает diff через GitHub API и публикует общий комментарий и замечания к строкам. Код из PR не скачивается и не запускается.
4. Ставит approve, если выполнены все условия:
   - вердикт модели `no_concerns_found`;
   - нет ни ключевых замечаний, ни проблем безопасности;
   - все изменённые файлы проверены полностью;
   - ни один изменённый файл не подпадает под `approval-excluded-paths` (по умолчанию `.github/*`: изменения CI одобряет человек);
   - пока шло ревью, последний коммит PR не изменился.

   Approve ставится на конкретный проверенный коммит. Во всех остальных случаях бот снимает (dismiss) свои прежние approve. Если ревью упало с ошибкой, бот тоже их снимает. Ревью людей он никогда не трогает.

Approve бота — это сигнал, а не гарантия. Модель может пропустить ошибку, а текст PR может пытаться её обмануть. Всё важное по-прежнему проверяет человек.

## Настройка

### Один раз на аккаунт: GitHub App для ревьюера

1. Откройте **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Задайте имя и логотип. В разделе **Webhook** снимите галочку **Active**.
3. В **Repository permissions** выставьте **Contents: Read-only**, **Issues: Read and write** и **Pull requests: Read and write**.
4. Создайте приложение. Запишите его **App ID** и сгенерируйте **private key**.
5. Чтобы пользоваться приложением в организации, включите **Advanced → Make public** и установите его на организацию. Приватный ключ при этом не раскрывается.

### Для каждого репозитория

1. **Install App** → добавьте репозиторий в установку.
2. В **Settings → Secrets and variables → Actions** добавьте:
   - секрет `DEEPSEEK_API_KEY`;
   - секрет `REVIEW_APP_PRIVATE_KEY`: PEM-файл целиком;
   - переменную `REVIEW_APP_ID`.

   В организации на платном тарифе или для публичных репозиториев их можно задать один раз на уровне организации. На GitHub Free приватные репозитории не видят секреты организации.
3. Добавьте файл `.github/workflows/deepseek-review.yml`:

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
          # Необязательно:
          # allowed-authors: alice, bob
          # approve: "false"
          # extra-instructions: |
          #   Check that every user-facing string exists in both locales.
```

`pull_request_target` запускает workflow из базовой ветки и даёт ему доступ к секретам, даже для PR из форков. Здесь это безопасно только потому, что код из PR не выполняется. Не добавляйте шаг, который скачивает и запускает код из PR.

## Требования к раннеру

Нужен Linux-раннер с Docker и Python 3. На раннерах GitHub `ubuntu-latest` есть и то и другое.

## Чьи PR проверяются

По умолчанию проверяются PR от владельцев репозитория, членов организации и коллабораторов. Каждое ревью тратит токены DeepSeek, а так посторонние не смогут расходовать ваш ключ. Чтобы перечислить конкретные логины, задайте `allowed-authors`. Скрытых членов организации GitHub иногда помечает как `CONTRIBUTOR`. Если так случится, впишите их в список явно.

## Параметры

| Параметр | По умолчанию | Значение |
|---|---|---|
| `deepseek-api-key` | обязателен | Ключ DeepSeek API |
| `app-id` или `client-id` | нужен один | Идентификатор GitHub App ревьюера |
| `app-private-key` | обязателен | Приватный ключ GitHub App ревьюера |
| `allowed-authors` | пусто | Логины для ревью; если пусто — владельцы, члены организации и коллабораторы |
| `approve` | `true` | Ставить approve чистым ревью и снимать устаревшие approve бота |
| `approval-excluded-paths` | `.github/*` | Маски файлов, которые бот никогда не одобряет, только человек. `*` совпадает и с `/`. Пусто — разрешить любые пути |
| `extra-instructions` | пусто | Правила проекта, которые добавляются к общей политике ревью |
| `ignore-globs` | пусто | JSON-массив масок файлов, которые не проверять, например `'["*.meta", "*.unity"]'`; такие файлы не ревьюятся и не мешают approve |
| `model` | `deepseek/deepseek-flash` | Имя модели в LiteLLM; это DeepSeek-V4.1-Flash |
| `max-model-tokens` | `512000` | Окно токенов на вызов; на вход остаётся то, что не занято `max-output-tokens` (по умолчанию 128K) |
| `max-output-tokens` | `384000` | Лимит выходных токенов на вызов, включая размышления модели; для моделей с меньшим лимитом уменьшите его вместе с `max-model-tokens` |
| `response-language` | `en-US` | Язык ревью |
| `max-findings` | `5` | Максимум замечаний |
| `pr-agent-image` | `pragent/pr-agent:0.49.0` | Образ pr-agent; шаг approve зависит от формата его `--json-output` |

Выходы: `reviewed` (`true`, если ревью было) и `approval` (`approved`, `already-approved`, `not-approved`, `head-changed`).

## Версии

Указывайте `@v1`, чтобы получать совместимые обновления, или полный SHA коммита, чтобы зафиксировать точную версию. Смена образа pr-agent или правил approve выходит новой major-версией.

## Разработка

```sh
python3 -m unittest discover -s tests -v
```

Контрактным тестам `action.yml` нужен PyYAML; без него они пропускаются.

Логика approve находится в `scripts/review_gate.py` и использует только стандартную библиотеку Python.


