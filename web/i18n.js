/* Panel language switch.
 *
 * The panel is written in Russian; English is produced at the output by
 * translating text nodes and labelling attributes as they appear, so app.js
 * stays a single source of strings. User content (names, prompts, answers)
 * is never in the dictionary and therefore passes through untouched.
 */
(() => {
  "use strict";

  const STORAGE_KEY = "openclaude-control-language";

  const EXACT = {
    "1 год": "1 year",
    "1 час": "1 hour",
    "24 часа": "24 hours",
    "30 дней": "30 days",
    "7 дней": "7 days",
    "90 дней": "90 days",
    "API ещё не готов": "API not ready yet",
    "Backend ещё не обнаружил доступные модели. Запустите проверку входа.": "The backend has not discovered the available models yet. Run a login check.",
    "Backend не вернул id созданного профиля": "The backend did not return the id of the created profile",
    "Backend пока не передал systems в снимке состояния.": "The backend has not reported components in its state snapshot yet.",
    "Claude генерирует ответ": "Claude is generating an answer",
    "Grok Web · экспериментально": "Grok Web · experimental",
    "Grok-транспорт не проверен": "Grok transport unverified",
    "IDE, код и проверка результата": "IDE, code and verifying the result",
    "System/developer-инструкции, tool arguments/results, cookies и идентификаторы аккаунта сюда не записываются.": "System/developer instructions, tool arguments/results, cookies and account identifiers are never written here.",
    "Живая телеметрия без сохранения текста ответа": "Live telemetry without storing the answer text",
    "socks5, http или https. Можно вставить строку целиком:": "socks5, http or https. A full provider line also works:",
    "socks5://логин:пароль@host:1080": "socks5://user:password@host:1080",
    "xAI отклонил автоматизированный браузер. Ручной Chrome на этом ПК работает.": "xAI rejected the automated browser. A manual Chrome on this PC works.",
    "· текст сохранён": "· text stored",
    "Автоматически": "Automatic",
    "Авторизация подтверждена": "Login confirmed",
    "Адрес": "Address",
    "Адрес моста": "Bridge address",
    "Аккаунт": "Account",
    "Активировать": "Activate",
    "Активируем…": "Activating…",
    "Активность": "Activity",
    "Активный профиль": "Active profile",
    "Активный профиль изменён": "Active profile changed",
    "Актёр": "Actor",
    "Базовый URL": "Base URL",
    "Без system/developer-инструкций и результатов инструментов": "Without system/developer instructions and tool results",
    "Без дополнительной информации": "No further details",
    "Без дополнительной роли": "No additional role",
    "Браузер запущен": "Browser started",
    "Браузерная сессия не готова": "Browser session not ready",
    "Будет создан диагностический профиль Chrome. Ручной Chrome на этом ПК работает, но xAI блокирует автоматизированные браузеры, поэтому Grok API останется выключен.": "A diagnostic Chrome profile will be created. A manual Chrome on this PC works, but xAI blocks automated browsers, so the Grok API stays disabled.",
    "Буфер обмена недоступен": "Clipboard unavailable",
    "В каталоге · доступ не подтверждён": "In catalog · access unconfirmed",
    "Версию прочитать не удалось:": "Could not read the version:",
    "Версия": "Version",
    "Взрослый тон": "Mature tone",
    "Включён эфемерный режим: метрики сохраняются, но тексты новых запросов в эту панель не дублируются.": "Ephemeral mode is on: metrics are kept, but the texts of new requests are not copied into this panel.",
    "Внимание": "Attention",
    "Войдите в аккаунт в открытом окне": "Log in to the account in the opened window",
    "Войти": "Log in",
    "Войти заново": "Log in again",
    "Время": "Time",
    "Время ответа": "Response time",
    "Все компоненты работают нормально": "All components are healthy",
    "Все модели": "All models",
    "Все настройки": "All settings",
    "Все провайдеры": "All providers",
    "Все профили": "All profiles",
    "Все статусы": "All statuses",
    "Все уровни": "All levels",
    "Всё время": "All time",
    "Вход в claude.ai": "Log in to claude.ai",
    "Вход виден, но completion-транспорт выключен": "Logged in, but the completion transport is off",
    "Вход отменён, браузер закрыт": "Login cancelled, browser closed",
    "Вход подтверждён, список пока пуст": "Login confirmed, the list is still empty",
    "Вход сохранён · API ещё не готов": "Login saved · API not ready yet",
    "Вход сохранён, но web-протокол провайдера ещё не проверен и API остаётся выключен.": "Login saved, but the provider's web protocol is unverified and the API stays off.",
    "Вход сохранён, но протокол этого web-провайдера ещё не проверен.": "Login saved, but this web provider's protocol is still unverified.",
    "Выберите основу и независимо включите нужные модификаторы. Они не отменяют правила провайдера.": "Pick a base and switch on modifiers independently. They do not override the provider's rules.",
    "Выбрана": "Selected",
    "Выбранный профиль": "Selected profile",
    "Выбрать": "Select",
    "Вывод ответа": "Answer output",
    "Вызовов инструментов": "Tool calls",
    "Выполняется": "Running",
    "Выпускать этот профиль через прокси": "Route this profile through a proxy",
    "Выход в сеть": "Network exit",
    "Готов принимать запросы": "Ready for requests",
    "Данных за выбранный период пока нет": "No data for the selected period yet",
    "Действия": "Actions",
    "Диагностика автоматизированного доступа…": "Checking automated access…",
    "Длительность": "Duration",
    "Для OpenClaude замените последнюю строку на openclaude --model claude-web.": "For OpenClaude, replace the last line with openclaude --model claude-web.",
    "Для аккаунта будет создан отдельный локальный профиль Camoufox.": "A separate local Camoufox profile will be created for the account.",
    "Для устойчивого образа описывайте персонажа и отношения как художественную сцену. Формулировки «реальный человек», «не ИИ» и «не упоминай ИИ» часто вызывают отказ Claude, поэтому OpenClaude превращает их в свойства вымышленного персонажа. Если нужны отношения, напишите прямо: «В этой сцене она — девушка собеседника». Явные 18+ детали в постоянной карточке могут блокировать даже нейтральные реплики.": "For a stable persona, describe the character and the relationship as a fictional scene. Phrases like “a real human”, “not an AI” and “never mention AI” often make Claude refuse, so OpenClaude turns them into traits of a fictional character. If you need a relationship, say it directly: “In this scene she is the user's girlfriend”. Explicit 18+ details in a permanent card can block even neutral replies.",
    "Добавить профиль": "Add profile",
    "Добавьте профиль, чтобы проверить модели": "Add a profile to check models",
    "Дополнительные режимы общения": "Additional conversation modes",
    "Доступ заблокирован": "Access blocked",
    "Доступ проверен для текущего аккаунта; модели с требованием подписки выбрать нельзя.": "Access verified for the current account; models that require a subscription cannot be selected.",
    "Доступна": "Available",
    "Доступность определяется отдельно для каждого авторизованного аккаунта.": "Availability is determined per authenticated account.",
    "Доступные модели": "Available models",
    "Если доступно": "If available",
    "Если настраиваете клиент сами или на другой машине.": "If you configure the client yourself or on another machine.",
    "Есть несохранённые изменения. Предпросмотр обновится после сохранения.": "There are unsaved changes. The preview updates after saving.",
    "Есть проблема": "There is a problem",
    "Журнал очищен": "Log cleared",
    "Журнал событий": "Event log",
    "Заблокирован провайдером": "Blocked by provider",
    "Завершён": "Completed",
    "Загружаем запрос…": "Loading request…",
    "Загружаем локальную телеметрию…": "Loading local telemetry…",
    "Загрузка…": "Loading…",
    "Задать поведение вручную": "Define the behaviour yourself",
    "Закрываем браузер…": "Closing browser…",
    "Закрыть": "Close",
    "Запрос": "Request",
    "Запросов ещё не было": "No requests yet",
    "Запросы": "Requests",
    "Запросы во времени": "Requests over time",
    "Здесь появится поток ответа, если backend передаёт состояние генерации.": "The answer stream appears here when the backend reports generation state.",
    "Здесь появится поток ответа, как только клиент обратится к мосту.": "The answer stream appears here once a client talks to the bridge.",
    "Здоров": "Healthy",
    "Информация": "Info",
    "История": "History",
    "История запросов": "Request history",
    "История пока пуста": "History is empty so far",
    "Как OpenClaude получает и показывает генерацию.": "How OpenClaude receives and shows the generation.",
    "Как направить кодового клиента на этот мост.": "How to point a coding client at this bridge.",
    "Карточка отправляется без преобразований.": "The card is sent without changes.",
    "Клиент обращается к мосту как к обычному Anthropic API. Ключ не проверяется: мост слушает только петлевой интерфейс.": "The client talks to the bridge as to a regular Anthropic API. The key is not checked: the bridge listens on loopback only.",
    "Клиент установлен": "Client installed",
    "Команды вручную": "Manual commands",
    "Компонент": "Component",
    "Компонент, сообщение или уровень": "Component, message or level",
    "Логи": "Logs",
    "Логин": "Username",
    "Локальная история OpenClaude не затрагивается. Temporary применяется только к удалённому чату claude.ai.": "OpenClaude's local history is unaffected. Temporary applies only to the remote claude.ai chat.",
    "Локальное хранение": "Local storage",
    "Локальные данные активности очищены": "Local activity data cleared",
    "Метаданные и точные usage сохраняются локально. Тексты запросов и ответов можно включить отдельно.": "Metadata and exact usage are stored locally. Request and answer texts can be enabled separately.",
    "Метрики": "Metrics",
    "Метрики, локальная история запросов и журнал работы шлюза.": "Metrics, local request history and the gateway log.",
    "Модели": "Models",
    "Модели не объявляются до проверенного транспорта": "Models are not announced until the transport is verified",
    "Модели появятся после проверки web-протокола; сейчас этот провайдер работает только в режиме входа.": "Models appear after the web protocol is verified; this provider currently supports login only.",
    "Модели станут доступны после авторизации.": "Models become available after login.",
    "Модель": "Model",
    "Модель активного профиля": "Active profile model",
    "Модель профиля изменена": "Profile model changed",
    "Модель, текст или ошибка": "Model, text or error",
    "Модификаторы": "Modifiers",
    "Можно включать вместе с любой основой, в том числе со своей карточкой.": "Can be combined with any base, including your own card.",
    "Мост через OpenAI-путь": "Bridge via the OpenAI path",
    "Название": "Name",
    "Например, Резервный": "For example, Backup",
    "Например: «В художественной сцене — девушка собеседника, 23 года, любит колу…»": "For example: “In a fictional scene — the user's girlfriend, 23, loves cola…”",
    "Напрямую": "Direct",
    "Настраиваем Claude Project…": "Setting up the Claude Project…",
    "Настроен на мост": "Configured for the bridge",
    "Настроить заново": "Configure again",
    "Настроить на мост": "Point at the bridge",
    "Настройки": "Settings",
    "Настройки локальной истории сохранены": "Local history settings saved",
    "Настройки поведения": "Behaviour settings",
    "Не авторизован": "Not logged in",
    "Не для всех компонентов есть данные": "Not every component has data",
    "Не настроен": "Not configured",
    "Не передана": "Not provided",
    "Не показывать": "Hide",
    "Не проверяется: браузерный доступ отклонён": "Not checked: browser access rejected",
    "Не сохранено": "Not saved",
    "Не сохранять temporary-чат в обычной истории": "Keep the temporary chat out of the regular history",
    "Не удалось настроить Claude Project для профиля.": "Could not set up the Claude Project for the profile.",
    "Не удалось обновить панель": "Could not refresh the panel",
    "Не удалось открыть запрос": "Could not open the request",
    "Недоступна аккаунту": "Unavailable to the account",
    "Неизвестная ошибка": "Unknown error",
    "Некоторые компоненты требуют внимания": "Some components need attention",
    "Нет активной генерации": "No active generation",
    "Нет данных": "No data",
    "Нет данных о компонентах": "No component data",
    "Нет данных о сервисе": "No service data",
    "Нет данных по моделям": "No model data",
    "Обзор": "Overview",
    "Обновить": "Refresh",
    "Общие режимы ответов для активного профиля.": "Shared answer modes for the active profile.",
    "Обычный": "Regular",
    "Один или несколько компонентов недоступны": "One or more components are unavailable",
    "Один ряд — один вызов API; связанные вызовы отмечены одинаковым хэшем сессии.": "One row is one API call; related calls share the same session hash.",
    "Ожидаем данные": "Waiting for data",
    "Ожидаем запуск": "Waiting for start",
    "Окно Camoufox открыто": "Camoufox window open",
    "Окно Chrome открыто": "Chrome window open",
    "Окно браузера закрыто": "Browser window closed",
    "Основной режим общения": "Primary conversation mode",
    "Оставлять чат в claude.ai": "Keep the chat in claude.ai",
    "Отдельный локальный браузерный профиль для каждого аккаунта.": "A separate local browser profile for every account.",
    "Отключить сохранение текстов и удалить уже сохранённые тексты? Метрики останутся.": "Stop storing texts and delete the ones already stored? Metrics will remain.",
    "Отменить и закрыть браузер": "Cancel and close the browser",
    "Отменён": "Cancelled",
    "Отправлять части ответа в OpenClaude сразу после получения.": "Send parts of the answer to OpenClaude as soon as they arrive.",
    "Очистить данные": "Clear data",
    "Очистить журнал": "Clear log",
    "Очистить локальный журнал событий? История запросов останется.": "Clear the local event log? Request history will remain.",
    "Ошибка": "Error",
    "Ошибки": "Errors",
    "Ошибки выделены красным; пустые интервалы не дорисовываются.": "Errors are red; empty intervals are not drawn.",
    "Пароли и cookies не покидают локальный профиль.": "Passwords and cookies never leave the local profile.",
    "Пароль": "Password",
    "Пароль не сохранён.": "No password stored.",
    "Пароль сохранён. Пусто — останется прежним.": "A password is stored. Leave empty to keep it.",
    "Период": "Period",
    "По моделям": "By model",
    "Поведение": "Behaviour",
    "Повторить": "Retry",
    "Подключение": "Connect",
    "Подключён": "Connected",
    "Подождите…": "Please wait…",
    "Поиск": "Search",
    "Показать ещё": "Show more",
    "Показывать": "Show",
    "Показывать thinking": "Show thinking",
    "Показывать только provider summary, если Claude её отдаёт. Скрытая цепочка мыслей недоступна.": "Show only the provider summary when Claude returns one. The hidden chain of thought is unavailable.",
    "Пользователь": "User",
    "После сохранения здесь появится фактическая карточка.": "The effective card appears here after saving.",
    "Последний ответ": "Last answer",
    "Последняя генерация": "Last generation",
    "Последняя проверка": "Last check",
    "Поток ответа появится здесь во время генерации.": "The answer stream appears here during generation.",
    "Потоковая выдача": "Streaming",
    "Предупреждения": "Warnings",
    "Прерван": "Interrupted",
    "Приватность": "Privacy",
    "Приватность чатов": "Chat privacy",
    "Провайдер": "Provider",
    "Провайдер / модель": "Provider / model",
    "Проверим после входа": "Checked after login",
    "Проверить": "Check",
    "Проверить выход": "Test exit",
    "Проверить доступ": "Check access",
    "Проверить снова": "Check again",
    "Проверяем авторизацию…": "Checking login…",
    "Проверяем компоненты…": "Checking components…",
    "Проверяем сервис…": "Checking service…",
    "Проверяем соединение…": "Checking the connection…",
    "Проверяем, пропускает ли xAI это автоматизированное окно": "Checking whether xAI lets this automated window through",
    "Программист": "Programmer",
    "Продолжение после результата инструмента. Содержимое инструмента намеренно не сохраняется.": "Continuation after a tool result. Tool content is deliberately not stored.",
    "Прокси": "Proxy",
    "Прокси не ответил": "The proxy did not answer",
    "Прокси профиля": "Profile proxy",
    "Прокси сохранён": "Proxy saved",
    "Прокси сохранён, браузер профиля перезапущен": "Proxy saved, the profile's browser restarted",
    "Профилей пока нет": "No profiles yet",
    "Профили": "Profiles",
    "Профили не найдены": "No profiles found",
    "Профиль": "Profile",
    "Профиль не выбран": "No profile selected",
    "Профиль не готов: аккаунт уже используется или был заменён.": "Profile not ready: the account is already in use or was replaced.",
    "Профиль подключён": "Profile connected",
    "Профиль, режимы и состояние локального шлюза.": "Profile, modes and the state of the local gateway.",
    "Прямой зрелый стиль; сам по себе не добавляет тему": "A direct, grown-up style; adds no topic by itself",
    "Пусто — сохранённый пароль останется прежним.": "Empty — the stored password stays as it is.",
    "Рабочая директория": "Working directory",
    "Разделы активности": "Activity sections",
    "Разделы панели": "Panel sections",
    "Размер ответа": "Answer size",
    "Режим общения": "Conversation mode",
    "Ручной Chrome доступен; управляемое окно отклонено xAI": "Manual Chrome works; the automated window was rejected by xAI",
    "Санитизированные события backend, сохраняемые между перезапусками.": "Sanitised backend events kept across restarts.",
    "Своя инструкция": "Custom instruction",
    "Сервис": "Service",
    "Сервис восстанавливается": "Service recovering",
    "Сервис запущен": "Service running",
    "Сервис недоступен": "Service unavailable",
    "Скопировано": "Copied",
    "Скопировать": "Copy",
    "Смотрит в другое место": "Points elsewhere",
    "Смотрит на": "Points at",
    "Сначала войдите в аккаунт этого профиля.": "Log in to this profile's account first.",
    "Событий пока нет": "No events yet",
    "Создать и открыть": "Create and open",
    "Создать и открыть Claude для входа": "Create and open Claude to log in",
    "Создать профиль и запустить диагностику": "Create the profile and run diagnostics",
    "Создаём профиль…": "Creating profile…",
    "Сообщение": "Message",
    "Состояние": "State",
    "Состояние клиентов обновлено": "Client state refreshed",
    "Состояние компонентов неизвестно": "Component state unknown",
    "Состояние профиля обновлено": "Profile state refreshed",
    "Состояние транспорта": "Transport state",
    "Сохранение перезапускает браузер активного профиля: иначе он продолжит ходить со старого адреса.": "Saving restarts the active profile's browser: otherwise it would keep using the old address.",
    "Сохранено": "Saved",
    "Сохранить и применить": "Save and apply",
    "Сохранить инструкцию": "Save instruction",
    "Сохраняем…": "Saving…",
    "Сохранять тексты диалогов": "Store dialogue texts",
    "Сохраняются метаданные, статусы и usage. Тексты запросов и ответов отключены.": "Metadata, statuses and usage are stored. Request and answer texts are off.",
    "Статус": "Status",
    "Текст не сохранялся для этого запроса.": "No text was stored for this request.",
    "Текст появляется сразу в OpenClaude": "Text appears in OpenClaude immediately",
    "Тексты диалогов": "Dialogue texts",
    "Тексты новых запросов и финальных ответов сохраняются только в локальной SQLite-базе.": "Texts of new requests and final answers are stored only in the local SQLite database.",
    "Текущий ответ": "Current answer",
    "Токены": "Tokens",
    "Токены суммируются только для запросов с upstream usage.": "Tokens are summed only for requests with upstream usage.",
    "Точные токены": "Exact tokens",
    "Удалить локальные метрики, тексты запросов и журнал? Чаты claude.ai и история самого OpenClaude не изменятся.": "Delete local metrics, request texts and the log? claude.ai chats and OpenClaude's own history are unaffected.",
    "Уровень": "Level",
    "Успешность": "Success rate",
    "Устанавливаем…": "Installing…",
    "Установить": "Install",
    "Установка идёт дольше обычного": "The installation is taking longer than usual",
    "Установка не удалась, подробности в карточке": "Installation failed, details in the card",
    "Установлено": "Installed",
    "Устойчивее держать выбранный образ и сцену": "Holds the chosen persona and scene more steadily",
    "Фильтры активности": "Activity filters",
    "Финальный текст ответа не сохранялся или отсутствовал.": "The final answer text was not stored or was empty.",
    "Хранить": "Keep",
    "Через этот адрес профиль выходит в сеть — и вход, и рабочая сессия.": "The profile reaches the network through this address — both login and the working session.",
    "Что будет отправлено Claude": "What will be sent to Claude",
    "Что делать с удалённым чатом после завершения ответа.": "What to do with the remote chat once the answer is finished.",
    "Эфемерный": "Ephemeral",
    "Эфемерный режим приватности отключает сохранение текстов. Смените приватность на «Обычный» во вкладке «Поведение».": "Ephemeral privacy disables text storage. Switch privacy to “Regular” on the Behaviour tab.",
    "активен": "active",
    "не определилась": "unknown",
    "не установлен": "not installed",
    "подписка": "subscription",
    "запросов с exact usage": "requests with exact usage",
    "Не авторизован — войдите в аккаунт": "Not logged in — log in to the account",
    "Как это работает": "How it works",
    "Готов к запросам": "Ready for requests",
    "Запускается": "Starting",
    "Восстанавливается": "Recovering",
    "Ждёт результат инструмента": "Waiting for a tool result",
    "Нужен вход в claude.ai": "Log in to claude.ai",
    "Аккаунт не подтверждён": "Account unconfirmed",
    "В браузере другой аккаунт": "A different account in the browser",
    "Готовит Claude Project": "Preparing the Claude Project",
    "Браузер недоступен": "Browser unavailable",
    "Остановлен": "Stopped",
    "Один аккаунт — один профиль": "One account, one profile",
    "У каждого профиля свой браузер Camoufox с отдельными cookies. Добавьте профиль, войдите в claude.ai в открывшемся окне — панель сама проверит вход и прочитает доступные модели.": "Every profile has its own Camoufox browser with separate cookies. Add a profile and log in to claude.ai in the window that opens — the panel verifies the login and reads the available models itself.",
    "Ротация при лимите": "Rotation on limits",
    "Когда активный аккаунт упирается в лимит, мост сам переключается на следующий готовый профиль и повторяет запрос. Ограниченный профиль отдыхает час и снова становится доступен.": "When the active account hits its limit, the bridge switches to the next ready profile by itself and replays the request. The limited profile rests for an hour and becomes available again.",
    "Свой выход для каждого": "A separate exit for each",
    "Кнопка «Прокси» задаёт профилю собственный socks5 или http(s)-адрес с логином и паролем. Вход и рабочая сессия идут через него, а таймзона и WebRTC подстраиваются под точку выхода.": "The “Proxy” button gives a profile its own socks5 or http(s) address with a username and password. Login and the working session go through it, and the timezone and WebRTC follow the exit point.",
  };

  /* Strings that app.js assembles with numbers or names. */
  const PATTERNS = [
    [/^Выход через (.+) · (\d+) мс$/, "Exit via $1 · $2 ms"],
    [/^Прокси · (.+)$/, "Proxy · $1"],
    [/^Войти в «(.+)»$/, "Log in to “$1”"],
    [/^Вход в (.+)$/, "Log in to $1"],
    [/^Открыть (.+)$/, "Open $1"],
    [/^Перезапусков: (\d+)$/, "Restarts: $1"],
    [/^Клиент настроен на мост; прежние настройки сохранены \((\d+)\)$/, "Client configured for the bridge; previous settings kept ($1)"],
    [/^Версию прочитать не удалось: (.+)$/, "Could not read the version: $1"],
    [/^Ошибка установки: (.+)$/, "Installation error: $1"],
    [/^Профиль (.+) пропущен: (.+)$/, "Profile $1 skipped: $2"],
    [/^Сократить срок до (\d+) дней\? Более старые локальные записи будут удалены\.$/, "Shorten retention to $1 days? Older local records will be deleted."],
    [/^профиль (.+)$/, "profile $1"],
    [/^(\d+) tool-вызовов$/, "$1 tool calls"],
    [/^(\d[\d\s.,]*) токенов$/, "$1 tokens"],
    [/^(\d[\d\s.,]*) символов$/, "$1 characters"],
    [/^(\d[\d\s.,]*) ток\/с$/, "$1 tok/s"],
    [/^(\d[\d\s.,]*) мс$/, "$1 ms"],
    [/^(\d[\d\s.,]*) с$/, "$1 s"],
    [/^(\d[\d\s.,]*) мин$/, "$1 min"],
    [/^(\d[\d\s.,]*) запросов$/, "$1 requests"],
    [/^(\d[\d\s.,]*) запросов · (\d[\d\s.,]*) ошибок$/, "$1 requests · $2 errors"],
    [/^(\d[\d\s.,]*) сессий · (\d[\d\s.,]*) запросов$/, "$1 sessions · $2 requests"],
    [/^(\d[\d\s.,]*) ошибок$/, "$1 errors"],
    [/^(\d[\d\s.,]*) отмен$/, "$1 cancelled"],
    [/^(\d[\d\s.,]*) из (\d[\d\s.,]*)$/, "$1 of $2"],
    [/^· ещё ≈ (.+)$/, "· ≈ $1 more"],
    [/^· сессия …(.+)$/, "· session …$1"],
    [/^(.+) · сессия …(.+)$/, "$1 · session …$2"],
    [/^(.+) заблокировал автоматизированное окно$/, "$1 blocked the automated window"],
    [/^(.+) отклонил автоматизированный браузер\. Ручной Chrome на этом ПК работает\.$/, "$1 rejected the automated browser. A manual Chrome on this PC works."],
    [/^OpenClaude переформулировал (\d+) конфликтных фрагментов, не меняя сохранённый исходник\.$/, "OpenClaude rephrased $1 conflicting fragments without changing the stored source."],
    [/^Требуется (.+)$/, "Requires $1"],
    [/^Этот аккаунт уже привязан к профилю «(.+)»\. Один аккаунт — один профиль: войдите здесь другим аккаунтом или удалите лишний профиль\.$/, "This account already belongs to the profile “$1”. One account, one profile: log in here with a different account or remove the extra profile."],
    [/^Этот аккаунт уже привязан к другому профилю\.$/, "This account already belongs to another profile."],
    [/^В браузере этого профиля теперь другой аккаунт\. Войдите прежним или создайте для нового отдельный профиль\.$/, "This profile's browser is now logged into a different account. Log back in with the previous one, or create a separate profile for the new one."],
    [/^Модели · (.+)$/, "Models · $1"],
    [/^(.+) направлен на мост$/, "$1 pointed at the bridge"],
  ];

  const ATTRIBUTES = ["placeholder", "title", "aria-label"];
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "PRE", "TEXTAREA"]);
  const CYRILLIC = /[Ѐ-ӿ]/;

  let language = "ru";
  try {
    language = localStorage.getItem(STORAGE_KEY) === "en" ? "en" : "ru";
  } catch {
    language = "ru";
  }

  const sources = new WeakMap();

  function translate(text) {
    if (language !== "en" || !text || !CYRILLIC.test(text)) return text;
    const leading = text.match(/^\s*/)[0];
    const trailing = text.match(/\s*$/)[0];
    // Markup wraps long sentences across lines; the dictionary keys do not.
    const core = text.trim().replace(/\s+/g, " ");
    let output = EXACT[core];
    if (output === undefined) {
      for (const [pattern, replacement] of PATTERNS) {
        if (pattern.test(core)) {
          output = core.replace(pattern, replacement);
          break;
        }
      }
    }
    return output === undefined ? text : leading + output + trailing;
  }

  function isSkipped(element) {
    for (let node = element; node; node = node.parentElement) {
      if (SKIP_TAGS.has(node.tagName)) return true;
    }
    return false;
  }

  function applyTextNode(node) {
    if (!node.parentElement || isSkipped(node.parentElement)) return;
    const record = sources.get(node);
    // A value we wrote ourselves is not a new source string.
    if (record && node.data === record.output) {
      if (record.language === language) return;
    } else {
      sources.set(node, { source: node.data, output: node.data, language });
    }
    const current = sources.get(node);
    const output = translate(current.source);
    current.output = output;
    current.language = language;
    if (node.data !== output) node.data = output;
  }

  function applyAttributes(element) {
    // A textarea's own text is user content, but its placeholder is ours.
    if (element.closest("script, style")) return;
    for (const name of ATTRIBUTES) {
      if (!element.hasAttribute(name)) continue;
      const key = `attr:${name}`;
      let record = sources.get(element);
      if (!record) {
        record = {};
        sources.set(element, record);
      }
      const value = element.getAttribute(name);
      if (!record[key] || value !== record[key].output) {
        record[key] = { source: value, output: value };
      }
      const output = translate(record[key].source);
      record[key].output = output;
      if (value !== output) element.setAttribute(name, output);
    }
  }

  function applyTree(root) {
    if (root.nodeType === Node.TEXT_NODE) {
      applyTextNode(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) applyAttributes(root);
    const walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
    );
    let node = walker.nextNode();
    while (node) {
      if (node.nodeType === Node.TEXT_NODE) applyTextNode(node);
      else applyAttributes(node);
      node = walker.nextNode();
    }
  }

  function renderToggle() {
    document.querySelectorAll("[data-language-toggle]").forEach((button) => {
      button.textContent = language === "en" ? "RU" : "EN";
      button.setAttribute(
        "aria-label",
        language === "en" ? "Переключить на русский" : "Switch to English",
      );
      button.title = button.getAttribute("aria-label");
    });
  }

  function setLanguage(next) {
    language = next === "en" ? "en" : "ru";
    try {
      localStorage.setItem(STORAGE_KEY, language);
    } catch {
      /* storage may be unavailable; the choice then lasts for the session */
    }
    document.documentElement.lang = language;
    applyTree(document);
    renderToggle();
  }

  const nativeConfirm = window.confirm.bind(window);
  window.confirm = (message) => nativeConfirm(translate(String(message)));

  window.openclaudeI18n = {
    get language() {
      return language;
    },
    translate,
    setLanguage,
  };

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") {
        applyTextNode(mutation.target);
      } else if (mutation.type === "attributes") {
        applyAttributes(mutation.target);
      } else {
        mutation.addedNodes.forEach(applyTree);
      }
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.documentElement.lang = language;
    applyTree(document);
    renderToggle();
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRIBUTES,
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-language-toggle]");
      if (!button) return;
      event.preventDefault();
      setLanguage(language === "en" ? "ru" : "en");
    });
  });
})();
