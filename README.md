# claude-web-api

[Русская версия ниже / Russian version below](#claude-web-api-ru)

A local bridge that serves the **Anthropic Messages API** and the **OpenAI Chat
Completions API** on `127.0.0.1`, and answers them from a real, logged-in
`claude.ai` session driven in a [Camoufox](https://github.com/daijro/camoufox)
browser profile. No official Anthropic, OpenAI or xAI API key is involved.

Point the official **Claude Code** at it with two environment variables, or any
OpenAI-compatible client at `/v1/chat/completions`, and keep your normal agent
loop: your client executes its own file, shell and search tools on your machine,
the bridge carries the conversation and the tool schemas to Claude in the
browser, and streams the answer back.

```text
client messages + tools
  -> POST /v1/messages            (or /v1/chat/completions)
  -> live SSE from claude.ai
  -> text and tool_use blocks
  -> the client runs the tool locally
  -> tool_result continues the same browser turn
```

## Why this exists

A web subscription and an API key are separate products. If you already pay for
the former and want a coding agent driven by it, the usual options are to paste
text by hand or to run something that fakes the API by guessing tool calls out
of the response text.

This bridge does neither. Tool calls come from Claude's own native `tool_use`
stream, and anything it cannot deliver fails visibly instead of being faked —
unsupported content returns a `400`, absent token counts are reported as zero,
and the experimental Grok provider reports no capabilities rather than pretending
to have them.

## Requirements

- Windows is the primary host (PowerShell entry points); the package itself is
  plain Python 3.12 and portable
- A Claude account you can log into in a browser
- ~500 MB of disk for the portable runtime and the Camoufox browser

## Install

One line in PowerShell — it fetches the repository, builds the portable runtime
and starts the bridge:

```powershell
irm https://raw.githubusercontent.com/beekamai/claude-web-api/main/install.ps1 | iex
```

It installs into `%USERPROFILE%\claude-web-api`, or `C:\claude-web-api` when the
user name contains non-ASCII characters (the portable runtime needs a plain
path; override with `$env:CLAUDE_WEB_API_DIR`), and re-running it updates an existing installation
without touching the browser profile, `control_config.json` or the journal. Set
`$env:CLAUDE_WEB_API_NO_START = "1"` to install without starting.

Manually, if you prefer:

```powershell
git clone https://github.com/beekamai/claude-web-api
cd claude-web-api
.\scripts\setup.ps1
```

`setup.ps1` fetches a portable Python 3.12, installs the package and downloads
the Camoufox browser. Nothing is installed system-wide.

## Run

```powershell
.\scripts\start.ps1
```

The first run opens a visible browser — log into `claude.ai` there. The session
is stored in `profile\`, and the main runtime goes headless afterwards.

```text
API:            http://127.0.0.1:8765
Control panel:  http://127.0.0.1:8765/control/
```

The server binds the loopback interface only, and is supervised: if the browser
dies or stalls it is restarted without losing the profile.

## Use it from Claude Code

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8765"
$env:ANTHROPIC_AUTH_TOKEN = "local-claude-web"
claude
```

Full setup, including a permanent `settings.json` entry and the list of what
works and what does not: [`docs/claude-code-setup.md`](docs/claude-code-setup.md).

## Use it from an OpenAI-compatible client

```powershell
$env:OPENAI_BASE_URL = "http://127.0.0.1:8765/v1"
$env:OPENAI_API_KEY = "local-claude-web"
$env:OPENAI_MODEL = "claude-web"
```

**OpenClaude** now speaks the Anthropic surface too, so it needs no patch:
point `ANTHROPIC_BASE_URL` at the bridge exactly as Claude Code does and pass
`--model claude-web`. The patch in
[`integrations/openclaude/`](integrations/openclaude/), pinned to an exact
upstream commit, applies only to the older OpenAI path.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/messages` | Anthropic Messages API, streaming and buffered |
| `POST /v1/messages/count_tokens` | Prompt size **estimate** (see below) |
| `POST /v1/chat/completions` | OpenAI chat completions, streaming and buffered |
| `GET /v1/models` | Models the signed-in account can actually select |
| `GET /health`, `/health/live`, `/health/ready` | Liveness and browser readiness |
| `GET /control/`, `/api/control/*` | Local control panel and its API |

## Control panel

`http://127.0.0.1:8765/control/` shows live request state, local metrics and a
request journal, the browser profiles and their models, and the behaviour
switches: streaming, thinking visibility, privacy mode, and the conversational
persona. Browser cookies, raw account and session UUIDs, system prompts, tool
payloads and hidden thinking data are never exposed through it.

Details: [`docs/configuration.md`](docs/configuration.md).

## Known limits

- These are private `claude.ai` web endpoints; they can change without notice.
- Images and documents cannot be carried by a browser turn — such content blocks
  are refused with `400` rather than silently dropped.
- `thinking` summaries are not returned as content blocks, because the bridge
  cannot sign them for replay. They are visible in the panel.
- `max_tokens` is validated but not enforced: the web session delivers a
  finished answer.
- `count_tokens` returns a character-length **estimate**. claude.ai exposes no
  tokenizer, and usage is reported only after a turn runs.
- Temporary chats do not change Anthropic's server-side retention policy.
- This is personal browser automation, not a production proxy.

## Development

```powershell
.\.python\python.exe -m unittest discover -s tests -t .
.\.python\python.exe -m ruff check .
```

229 tests, no browser required. Conventions, layout and the traps that bite
newcomers are in [`AGENTS.md`](AGENTS.md).

## License

MIT

---

# claude-web-api (RU)

Локальный мост: отдаёт **Anthropic Messages API** и **OpenAI Chat Completions
API** на `127.0.0.1`, а отвечает на них из живой авторизованной сессии
`claude.ai` в браузерном профиле [Camoufox](https://github.com/daijro/camoufox).
Никаких официальных ключей Anthropic, OpenAI или xAI.

Официальный **Claude Code** подключается двумя переменными окружения, любой
OpenAI-совместимый клиент — через `/v1/chat/completions`. Агентный цикл
остаётся у клиента: он сам выполняет файловые, shell- и поисковые инструменты
на вашей машине, а мост доносит до Claude в браузере переписку и схемы
инструментов и возвращает ответ потоком.

## Зачем это нужно

Веб-подписка и API-ключ — разные продукты. Если подписка уже оплачена, а нужен
кодовый агент на ней, обычно остаётся либо копировать текст руками, либо
запускать нечто, что подделывает API, угадывая вызовы инструментов по тексту
ответа.

Здесь ни того, ни другого. Вызовы инструментов приходят из настоящего нативного
потока `tool_use`, а всё, что мост отдать не может, падает заметно, а не
подделывается: неподдерживаемый контент — `400`, отсутствующий счётчик токенов —
честный ноль, экспериментальный Grok-провайдер сообщает об отсутствии
возможностей вместо того, чтобы их изображать.

## Установка

Одна строка в PowerShell — скачает репозиторий, соберёт переносимое окружение
и запустит мост:

```powershell
irm https://raw.githubusercontent.com/beekamai/claude-web-api/main/install.ps1 | iex
```

Ставится в `%USERPROFILE%\claude-web-api`, а если в имени пользователя есть
кириллица — в `C:\claude-web-api` (переносимому окружению нужен путь без
не-ASCII символов; задать своё — `$env:CLAUDE_WEB_API_DIR`).
Повторный запуск обновляет установку и не трогает браузерный профиль,
`control_config.json` и журнал. `$env:CLAUDE_WEB_API_NO_START = "1"` — поставить
без запуска.

Вручную:

```powershell
git clone https://github.com/beekamai/claude-web-api
cd claude-web-api
.\scripts\setup.ps1
```

`setup.ps1` скачивает portable Python 3.12, ставит пакет и браузер Camoufox.
В систему ничего не устанавливается.

## Запуск

```powershell
.\scripts\start.ps1
```

При первом запуске откроется видимый браузер — войдите там в `claude.ai`.
Сессия сохранится в `profile\`, дальше рантайм работает headless.

```text
API:     http://127.0.0.1:8765
Панель:  http://127.0.0.1:8765/control/
```

Сервер слушает только петлевой интерфейс и работает под супервизором: если
браузер умер или завис, он перезапускается без потери профиля.

## Подключение Claude Code

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8765"
$env:ANTHROPIC_AUTH_TOKEN = "local-claude-web"
claude
```

Полная инструкция и список ограничений —
[`docs/claude-code-setup.md`](docs/claude-code-setup.md). **OpenClaude**
подключается так же: те же две переменные и `--model claude-web`, патч из
`integrations/` для этого не нужен.

## Панель управления

Показывает текущий запрос, локальные метрики и журнал, профили и их модели,
режимы поведения: стриминг, видимость thinking, приватность, персона общения.
Cookies браузера, сырые UUID аккаунта и сессии, системные промпты, содержимое
инструментов и скрытые thinking-данные через панель не выдаются.

Подробности — [`docs/configuration.md`](docs/configuration.md).

## Ограничения

- Используются приватные web-endpoint'ы `claude.ai`; они могут измениться.
- Картинки и документы браузерный ход не переносит — такие блоки отклоняются
  с `400`, а не теряются молча.
- `thinking` не выдаётся блоками контента: мост не может подписать summary для
  переигрывания. В панели он виден.
- `max_tokens` валидируется, но не ограничивает: web-сессия отдаёт готовый ответ.
- `count_tokens` возвращает **оценку** по длине текста: токенизатора у claude.ai
  нет, а usage приходит только после хода.
- Temporary chat не меняет серверную политику хранения Anthropic.
- Это персональная браузерная автоматизация, а не production-прокси.

## Разработка

```powershell
.\.python\python.exe -m unittest discover -s tests -t .
.\.python\python.exe -m ruff check .
```

229 тестов, браузер не нужен. Конвенции и грабли — в [`AGENTS.md`](AGENTS.md).

## Лицензия

MIT
