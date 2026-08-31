# OpenClaude Web API (browser profiles)

Локальный OpenAI-совместимый мост между OpenClaude и авторизованными
web-сессиями моделей. OpenClaude остаётся IDE/host coordinator: он хранит
локальную сессию и выполняет файловые, shell- и прочие host tools, а мост
передаёт модели IDE-контекст и доступные ей нативные tool-схемы.

Официальные OpenAI, Anthropic и xAI API намеренно не используются. Каждый
аккаунт живёт в отдельном persistent browser-профиле; cookies и browser auth
не экспортируются в Python HTTP-клиент. Claude работает через Camoufox. Для
Grok подготовлен отдельный профиль установленного Google Chrome, но xAI сейчас
блокирует и Camoufox, и автоматизированный Playwright Chrome; ручной Chrome на
том же IP работает. Поэтому Grok остаётся fail-closed и не выдаётся за готовый
API.

```text
OpenClaude messages + tools
→ POST /v1/chat/completions
→ live SSE claude.ai
→ OpenAI text/reasoning/tool_call deltas
→ локальное выполнение tool в OpenClaude
→ следующий запрос с tool result
→ продолжение того же Claude Web turn
```

Сервер не подменяет ответы Claude заглушками и не угадывает команды по тексту.
Стабильный IDE-контракт из `project_instructions.txt` синхронизируется в
нативный `Project.prompt_template`. Текущая рабочая папка и выбранная модель
передаются request-scoped в описании первого нативного OpenClaude tool. Persona
фиксируется одним snapshot на весь запрос, дублируется там для аудита и
передаётся как явная пользовательская настройка в текущем user turn — поэтому
Claude не принимает её за случайные host metadata.
Если исполняемых client tools нет, сервер добавляет informational-only
`openclaude_host_context`; случайный вызов этого carrier обрабатывается внутри
моста и никогда не выдаётся клиенту как файловая операция. Project Instructions
не переписываются между запросами. Неподдерживаемое claude.ai web-поле
`custom_system_prompt` намеренно не используется.
Перед native turn мост проверяет стабильный Project prompt, а при запуске
автоматически исправляет только собственный устаревший dynamic-marker прежних
версий. Любое другое внешнее изменение сохраняется и останавливает отправку до
явного разрешения конфликта. Поле human `prompt` содержит только настоящий
запрос пользователя, а function schemas идут своим нативным каналом `tools`.
Для каждого аккаунта создаётся отдельный Claude Project `OpenClaude IDE`.

## Установка и запуск

Проект уже развёрнут с portable Python в `.python\`.

```powershell
cd D:\CodeWorks\claude-web-api
.\start.ps1
```

`start.ps1` запускает внешний supervisor и печатает адреса API и панели
управления. Если для основного профиля Claude запросит вход, авторизуйтесь в
открывшемся Camoufox; browser state сохраняется в `profile\`.

Повторная установка зависимостей и Camoufox:

```powershell
.\setup.ps1
```

Панель управления можно открыть вручную или скриптом:

```powershell
.\open-control.ps1
```

По умолчанию она доступна только локально:

```text
http://127.0.0.1:8765/control/
```

В панели видны health, текущий запрос, локальные метрики и история, профили,
доступные модели и режимы поведения. Browser cookies, raw account/session UUID,
system/developer prompts, tool payloads и скрытые thinking/signature данные
через панель не выдаются.

## Browser-провайдеры

Профиль всегда содержит явный `provider`:

| Provider ID | Сайт | Состояние |
|---|---|---|
| `claude_web` | `claude.ai` | Полный native tool loop, live SSE, thinking summary, модели и ротация профилей |
| `grok_web` | `grok.com` | Экспериментальный persistent Google Chrome; xAI блокирует automated browser, поэтому completion выключен до подтверждённого доступа, web-stream и tool continuation |

Grok не эмулирует tools текстовым промптом и не получает фиктивную
доступность моделей. До подтверждённого browser trace capabilities честно
равны `streaming=false`, `thinking=false`, `tool_continuation=unsupported`, а
попытка активации отклоняется. Это fail-closed граница, а не заглушка с
самодельным ответом. Проект не скрывает automation-флаги и не обходит
Cloudflare challenge.

В панели **Профили → Добавить профиль** сначала выбирается провайдер. Для Claude
открывается видимый Camoufox, для Grok — отдельный видимый Google Chrome.
Основной Claude runtime после входа снова остаётся headless. Grok Project не
создаётся: это Claude-специфичная сущность.

## OpenClaude

OpenClaude можно направить на мост как на OpenAI API:

```powershell
$env:CLAUDE_CODE_USE_OPENAI="1"
$env:OPENAI_API_KEY="local-claude-web"
$env:OPENAI_BASE_URL="http://127.0.0.1:8765/v1"
$env:OPENAI_MODEL="claude-web"

openclaude --provider openai --model claude-web
```

### Воспроизводимый патч OpenClaude

Изменения самого OpenClaude сохранены в
`integrations\openclaude\`. Пакет закреплён на точном upstream commit
`a3dc345f12d41b171cdb5cb74c1304b6cca483d8` и честно помечен как
`0.25.0-main.a3dc345-claudeweb.3`, а не как официальный релиз `v0.25.0`.

```powershell
git clone https://github.com/Gitlawb/openclaude.git
git -C .\openclaude checkout a3dc345f12d41b171cdb5cb74c1304b6cca483d8

pwsh -File .\integrations\openclaude\Install-OpenClaudePatch.ps1 `
  -Mode Check -SourcePath .\openclaude
pwsh -File .\integrations\openclaude\Install-OpenClaudePatch.ps1 `
  -Mode Install -SourcePath .\openclaude
```

Установщик сверяет commit и хэши пятнадцати файлов, применяет patch без fuzzy/3-way,
запускает затронутые тесты, typecheck и build, собирает `.tgz`, ставит его через
npm и только затем обновляет настройки streaming. До изменений он сохраняет
глобальный пакет и `settings.json`; откат:

```powershell
pwsh -File .\integrations\openclaude\Install-OpenClaudePatch.ps1 -Mode Rollback
```

Полная инструкция и точный manifest находятся в
`integrations\openclaude\README.md`.

Для OpenAI-совместимого профиля рекомендуется `ENABLE_TOOL_SEARCH=false`, чтобы
OpenClaude передавал исполняемые схемы сразу, а не откладывал их через
Anthropic-specific ToolSearch. OpenAI function schemas передаются в
`completion.tools` без урезания описаний,
примеров или JSON Schema annotations. Нативные `tool_use.id`, имена и аргументы
Claude возвращаются клиенту без переписывания. OpenClaude выполняет tool
локально и отправляет результат следующим OpenAI-совместимым запросом; сервер
привязывает незавершённый native turn к
`X-OpenClaude-Session-Id` (legacy alias: `X-Claude-Code-Session-Id`), чтобы
результат из другой клиентской сессии не был принят случайно. Патч OpenClaude
также отправляет `X-OpenClaude-Working-Directory`; для старых клиентов мост
понимает строки `CWD:` и `Primary working directory:` из system prompt.

claude.ai browser sandbox не считается локальным workspace. Текущая папка и
инструкции клиента передаются через нативные tool descriptions, а реальные
файловые и shell-операции выполняет OpenClaude. После первого completion мост
проверяет metadata разговора и останавливает запрос, если чат не привязан к
ожидаемому Project или temporary-режим не применился.

Последний подтверждённый хэш постоянного OpenClaude-контракта хранится локально
в `project_prompt_leases.json`. Поэтому новая версия может заменить только
собственный предыдущий контракт; внешняя правка Claude Project instructions
по-прежнему сохраняется и блокирует автоматическую перезапись.

## Настоящий streaming

При `stream: true` текстовые дельты claude.ai передаются клиенту сразу по мере
прихода, а не нарезаются после готового ответа. Нативные tool calls собираются
до корректной границы блока, после чего выдаются в OpenAI SSE с исходными
идентификаторами и аргументами.

Пример:

```powershell
curl.exe -N http://127.0.0.1:8765/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"claude-web","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"Ответь одним коротким предложением"}]}'
```

`stream_options.include_usage: true` добавляет финальный usage chunk **только**
если claude.ai действительно прислал token usage. Если upstream usage
отсутствует, сервер не подставляет нули и не выдаёт оценку за точное количество
токенов. В панели приблизительный live-счётчик, когда он нужен, явно помечен как
оценка.

Переключатель Streaming в панели разрешает или запрещает live-проброс. При
выключенном live-пробросе запрос с `stream: true` всё равно получает валидный
OpenAI SSE, но уже после завершения native turn.

## Локальные метрики, история и журнал

Раздел **Активность** сохраняет bounded-телеметрию в локальной SQLite-базе:

```text
.runtime\telemetry.sqlite3
```

Там есть статусы и длительность запросов, начальный и фактический профиль/
модель, tool-call count, санитизированные ошибки и token usage. Точные input /
output tokens суммируются только когда claude.ai действительно прислал оба
значения. Оценка `output_chars / 4` хранится отдельно и всегда показывается с
`≈`; она никогда не подмешивается к точным суммам.
Для Anthropic usage поле input считается как сумма обычного input,
`cache_read_input_tokens` и `cache_creation_input_tokens`; cache-read также
остаётся отдельной деталью usage.

По умолчанию база работает в metadata-only режиме. Переключатель **Тексты
диалогов** включает сохранение только текущего пользовательского ввода и
финального ответа Claude. Полные `messages`, system/developer instructions,
tool arguments/results, cookies, opaque session ID и account/org/conversation
UUID не сохраняются. Сессия группируется необратимым salted SHA-256 digest.
Телеметрия начинается с момента установки этой версии: старые чаты claude.ai
и JSONL OpenClaude автоматически в базу не импортируются.

Retention по умолчанию — 30 дней и максимум 5000 запросов. Его можно изменить
в панели. Отключение текстов удаляет уже сохранённые тела, а кнопка очистки
удаляет всю локальную телеметрию; ни одна из этих операций не удаляет историю
claude.ai или JSONL самой OpenClaude.

## Thinking

Доступны три режима:

- `off` — отправлять нативное `thinking_mode: "off"`, удалять `effort` и не
  отдавать thinking;
- `auto` — позволить claude.ai выбрать режим;
- `show` — запросить extended thinking и передавать доступные summary deltas.

Если claude.ai присылает **provider-generated thinking summary**, она доступна
OpenAI-клиенту в `delta.reasoning_content` (или в
`message.reasoning_content` для non-stream ответа). Это не raw chain of
thought: скрытые рассуждения, пустые/обрезанные thinking blocks и signatures
сервер не восстанавливает, не фабрикует и не логирует. Наличие summary зависит
от модели, аккаунта и ответа upstream.

Поле запроса `reasoning_effort` передаётся в поддерживаемый claude.ai effort;
недоступные аккаунту возможности остаются ограничены самим провайдером.

## Профили и авторизация

Предпочтительный способ добавить аккаунт Claude:

1. Открыть `/control/` и нажать добавление профиля.
2. Выбрать `Claude Web` и задать локальное имя профиля.
3. Запустить вход — откроется отдельный **видимый** Camoufox.
4. Войти в Claude и дождаться проверки в панели.
5. Сервер проверит авторизацию через Claude `/api/account`, получит разрешённый
   список моделей и создаст/переиспользует Claude Project `OpenClaude IDE` для
   этого аккаунта.
6. Активировать готовый профиль.

Пункт `Grok Web · экспериментально` сейчас запускает только диагностическое
окно установленного Chrome. Ручной Chrome на этом ПК и IP открывает grok.com,
но xAI отклоняет и Camoufox, и управляемый Playwright Chrome. Поэтому эта
диагностика не обещает вход, получение моделей или активацию профиля:
Grok-профиль остаётся `provider_blocked` либо `protocol_unverified`, а
completion API — выключенным.

Новый persistent browser profile создаётся в `profiles\<profile-id>\`.
`control_config.json` хранит настройки, masked identity и salted fingerprint
для обнаружения повторно добавленного аккаунта, но не cookies и не raw UUID.
Сами каталоги `profile\` и `profiles\` содержат чувствительное browser state:
их нельзя публиковать или передавать третьим лицам.

Окно добавления профиля можно явно закрыть кнопкой отмены. Если его забыли
открытым, enrollment-процесс завершит отдельный браузер автоматически через
`CLAUDE_ENROLLMENT_TTL` секунд (по умолчанию 900).

При account usage limit сервер помечает текущий профиль временно ограниченным и
ротирует только по готовым, включённым и неограниченным профилям. Автоповтор
разрешён лишь до появления видимого вывода или начала tool path. Неоднозначный
turn после вывода не повторяется, чтобы не выполнить `Bash`, `Write` и другие
действия дважды. Если подходящих профилей не осталось, API возвращает `429`.

`CLAUDE_PROFILE_DIRS` по-прежнему поддерживается как legacy bootstrap для
существующих каталогов:

```powershell
$env:CLAUDE_PROFILE_DIRS="D:\ClaudeProfiles\account-1;D:\ClaudeProfiles\account-2"
```

Для новых профилей рекомендуется панель: она также создаёт обязательный
per-profile Project и валидирует аккаунт.

## Модели

Список Sonnet, Haiku, Opus и других вариантов читается из model selector
авторизованного аккаунта. Недоступные, disabled, legacy и deprecated варианты
не считаются выбираемыми. Выбор хранится отдельно для каждого профиля и
проверяется против его фактического списка.

Общий `claude_ai_bootstrap_models_config` используется только как каталог для
отображения. Он не подтверждает право аккаунта на модель: до получения свежего
account-scoped selector такая строка помечается как непроверенная и не может
быть выбрана. Причина `upgrade_required` показывается в панели вместе с
требуемым тарифом.

- `claude-web` или `auto` — использовать выбранную для активного профиля модель
  либо автоматический выбор claude.ai;
- конкретный model ID — запросить эту модель, только если она доступна аккаунту;
- `GET /v1/models` — получить OpenAI-совместимый список известных доступных
  моделей.

## Privacy: обычные и temporary chats

Режим Privacy управляет удалённым чатом claude.ai:

- `keep` — обычный Project chat, видимый в истории аккаунта;
- `ephemeral` — чат остаётся привязанным к проверенному IDE Project (поэтому
  Project Instructions имеют системный приоритет), а `is_temporary: true` не
  даёт IDE-turn засорять обычную историю чатов claude.ai.

Это не browser private mode: persistent Camoufox profile и авторизация
сохраняются. Temporary относится к удалённому чату; дополнительно локальная
панель не дублирует тексты новых temporary-запросов в telemetry.sqlite3.
Локальная сессия самой OpenClaude продолжает храниться на машине, обычно в:

```text
%USERPROFILE%\.openclaude\projects\<project>\<session-id>.jsonl
```

Поэтому после remote temporary chat OpenClaude всё ещё может восстановить свою
локальную историю. Политика хранения данных на стороне Anthropic определяется
самим провайдером.

## Режимы общения

Основной режим добавляется поверх неизменяемого IDE/tool contract:

- `default` — без дополнительной роли;
- `programmer` — инженерный IDE-режим;
- `custom` — пользовательская инструкция.

`actor` и `mature` — независимые boolean-модификаторы, а не взаимоисключающие
персоны. Их можно одновременно включить поверх `default`, `programmer` или
`custom`: первый усиливает непрерывность художественного образа, второй задаёт
прямой взрослый регистр, но сам по себе не добавляет 18+ тему, персонажа,
отношения или сцену. Тему задаёт текущая реплика пользователя, а допустимость
ответа всё равно определяет провайдер. Сам по себе `actor` не придумывает
персонажа или отношения: они должны быть заданы карточкой либо уже существовать
в текущем диалоге.
Конфигурация v2 мигрирует в эту схему автоматически; если в старой конфигурации
был выбран `actor`/`mature` и сохранён непустой custom-текст, он восстанавливается
как базовая custom-card, а прежний режим становится соответствующей галочкой.

Сохранение custom persona в панели одновременно активирует режим `custom`.
Персона передаётся как обычная выбранная пользователем character card, а не как
псевдосистемная инструкция. Если карточка описывает персонажа или отношения,
OpenClaude просит Claude продолжить художественную диалоговую сцену от первого
лица; если это рабочий стиль, применить его как практическое руководство к
ответу. Это best effort поверх неизменяемого system prompt claude.ai, а не
жёсткая гарантия поведения модели. Карточка сохраняется при browser recovery,
смене чата и ротации профиля. Переключение на `default` отменяет сохранённую
custom-card даже при оставленных `actor`/`mature`; нейтральный reset передаётся
в semantic turns как служебное transport-состояние и не предназначен для
озвучивания моделью. На явно помеченный `OOC` вопрос либо конкретный вопрос о
фактической модели/provider, физическом теле или существовании вне чата
ожидается фактический ответ. Обычные «кто ты?» и вопросы об отношениях остаются
внутри сцены, когда карточка определяет ответ. Сценические действия не выдаются
за реально совершённые физические действия; tool results и факты не
выдумываются. Persona не разрешает выдумывать выполненные команды или
результаты host tools.

Исходный custom-текст хранится без изменений. Перед отправкой узкий компилятор
переформулирует только конфликтные требования вроде «реальный человек»,
«не робот» и «не упоминай ИИ» как свойства человека-персонажа внутри
художественной сцены. Это сохраняет задуманный образ, не требуя от Claude
скрывать буквальную техническую идентичность. Отношения не выводятся из слова
«девушка»: для них в карточке нужно явно написать, например, «В этой сцене она
— девушка собеседника». Панель хранит исходник отдельно и показывает
provider-facing effective preview вместе со списком точечных преобразований.

OpenClaude не может удалить, прочитать или заменить скрытый provider-owned
prompt claude.ai. Claude всё ещё может проигнорировать карточку, добавить
оговорку или выйти из роли. Bridge всегда возвращает настоящий ответ Claude:
он не подменяет отказ заглушкой и не переписывает результат post-processing.

`mature` не является jailbreak: он не отменяет правила Anthropic, защиту
несовершеннолетних, требования согласия или host-tool policy.

## API

Основные endpoint'ы:

| Endpoint | Описание |
|---|---|
| `GET /health` | Camoufox, watchdog, masked account и активный профиль |
| `GET /health/live` | Liveness event loop и внутреннего watchdog |
| `GET /health/ready` | Неблокирующий readiness snapshot Camoufox |
| `POST /chat` | Простой текстовый чат |
| `POST /new` | Принудительно открыть новый Claude Web chat |
| `POST /v1/chat/completions` | OpenAI Chat Completions, tools и live SSE |
| `GET /v1/models` | Доступные модели активного аккаунта + alias `claude-web` |
| `GET /control/` | Локальная панель управления |

Control API, который использует панель:

| Endpoint | Описание |
|---|---|
| `GET /api/control/state` | Конфигурация, health, activity и protocol snapshot |
| `GET /api/control/telemetry` | Метрики, страницы запросов и persistent events |
| `GET /api/control/telemetry/{request_id}` | Детали одной локальной записи |
| `PATCH /api/control/telemetry/settings` | Тексты диалогов и retention |
| `DELETE /api/control/telemetry` | Очистить локальные метрики, историю и события |
| `PATCH /api/control/behavior` | Streaming, thinking, privacy и persona |
| `POST /api/control/profiles` | Создать каталог и запись нового профиля |
| `POST /api/control/profiles/{id}/login` | Claude: открыть окно входа; Grok: запустить экспериментальную диагностику browser-доступа |
| `GET /api/control/profiles/{id}/login` | Claude: проверить вход, модели и Project; Grok: получить fail-closed результат диагностики |
| `DELETE /api/control/profiles/{id}/login` | Отменить окно авторизации |
| `POST /api/control/profiles/{id}/activate` | Переключить готовый Claude-профиль; непроверенный Grok отклоняется |
| `POST /api/control/profiles/{id}/model` | Выбрать проверенную модель профиля |
| `DELETE /api/control/events` | Очистить безопасный журнал панели |

Простой non-stream запрос:

```powershell
curl.exe -X POST http://127.0.0.1:8765/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"claude-web","messages":[{"role":"user","content":"Привет"}]}'
```

## Самовосстановление Camoufox

`start.ps1` запускает внешний supervisor. Он проверяет `/health/live` и при
трёх последовательных сбоях завершает только точное дерево дочернего server
PID (`python → Playwright → Camoufox`), затем перезапускает его с backoff и
circuit breaker. Внутри сервера отдельный watchdog:

- проверяет composer, закрытие/crash страницы и возраст активной операции;
- перезапускает браузер с тем же persistent profile при безопасном
  idle/pre-submit сбое;
- ограничивает зависающие DOM и `/tool_result` вызовы локальными deadline;
- заново сверяет `/api/account` непосредственно перед отправкой IDE prompt или
  tool result и блокирует turn при незаметной смене аккаунта;
- публикует phase, restart count и последнюю причину recovery в `/health`.

После отправки `Enter` или начала передачи tool result исход операции может
быть неоднозначным. Такой запрос **не повторяется автоматически**: API
возвращает `409` с `operation_id`, поднимает чистую browser session и требует
восстановить IDE-turn из фактической истории. Это защищает локальные действия
от двойного выполнения.

Если локальный watchdog OpenClaude завершил shell-команду уже после того, как
Claude открыл native `tool_use`, новый настоящий пользовательский turn считается
явным прерыванием старого tool-loop. Сервер атомарно отказывается от ожидающего
side-channel, открывает свежий Project chat, переносит bounded IDE-историю
вместе с сохранённым preview результата и сразу обрабатывает новое сообщение.
Чистый `tool_result` без нового пользовательского текста по-прежнему продолжает
тот же Claude stream.

При лимите длины разговора сервер открывает новый Project/temporary chat и
переносит ограниченную историю IDE, если повтор безопасен.

## Настройки окружения

```text
CLAUDE_HEADLESS=1
CLAUDE_PROFILE_DIRS=<legacy profile paths separated by ; on Windows>
CLAUDE_PROJECT_ID=<legacy/explicit project uuid>
CLAUDE_START_URL=<explicit claude.ai URL>
CLAUDE_DEBUG_REQUESTS=1
CLAUDE_WATCHDOG_INTERVAL=10
CLAUDE_WATCHDOG_PROBE_TIMEOUT=4
CLAUDE_WATCHDOG_STALL_TIMEOUT=90
CLAUDE_BROWSER_CLOSE_TIMEOUT=10
CLAUDE_BROWSER_START_TIMEOUT=330
CLAUDE_TOOL_RESULT_POST_TIMEOUT=20
CLAUDE_HUMANIZE_SECONDS=0.25
CLAUDE_ENROLLMENT_TTL=900
CLAUDE_RESTART_WINDOW=600
CLAUDE_RESTART_LIMIT=5
PORT=8765
```

Управляемые через UI настройки и профили сохраняются в
`control_config.json`. `claude_project.json` остаётся legacy-настройкой
основного профиля; для добавленных аккаунтов используются их собственные
Project ID.

## Резервные копии и возврат на GLM

Перед добавлением control center создан снимок:

```text
D:\CodeWorks\claude-web-api\.backups\before-control-center-20260726-000256
```

Перед ручным восстановлением остановите supervisor. Возвращайте из снимка
только нужные файлы проекта; каталоги browser profiles и свежий
`control_config.json` не перезаписывайте, если хотите сохранить добавленные
аккаунты.

Конфигурация OpenClaude до переключения с GLM сохранена отдельно. Для возврата
к GLM используйте:

```powershell
& "C:\Users\Jaros\.openclaude\backups\before-claude-web-openai-20260725-043753\restore-glm.ps1"
```

Этот скрипт меняет настройки клиента OpenClaude; browser profiles
claude-web-api при этом можно оставить для последующих тестов.

## Ограничения

- Используются приватные web endpoints claude.ai; они могут измениться.
- Полный raw chain of thought недоступен; наружу передаются только summary,
  которые фактически прислал провайдер.
- Точный token usage показывается только при наличии upstream usage.
- Temporary chat не отменяет локальное хранение сессии самой OpenClaude и не
  меняет серверную политику retention Anthropic.
- `secure_delete` удаляет SQLite-страницы и WAL, но не может гарантировать
  физическое стирание блоков на SSD.
- Это персональная browser automation, а не production proxy.
