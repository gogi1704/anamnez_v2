# «Консилиум»: установка через GitHub и Docker

Инструкция сделана по аналогии с `bitrix_connector` и учитывает текущую схему
сервера:

- `anketa_bot_max` уже использует `127.0.0.1:8000`;
- `bitrix_connector` уже использует `127.0.0.1:8001`;
- «Консилиум» будет использовать новый порт `127.0.0.1:8002`;
- внешний HTTPS-трафик принимает существующий Nginx;
- рекомендуемый адрес: `https://consilium.chelovecbitmax.ru`.

Старые контейнеры, их `.env`, каталоги и Nginx-конфигурации изменять не
требуется.

> По обычной ссылке теперь открывается экран идентификации: Telegram, MAX или
> осознанный анонимный вход. До подключения ботов кнопки мессенджеров покажут
> понятное сообщение, а демонстрацию можно продолжить анонимно. Анонимные данные
> остаются привязаны только к cookies текущего браузера.

## 0. Локальная проверка на Windows

### 0.1. Подготовить настройки

Откройте PowerShell в каталоге проекта:

```powershell
cd C:\Users\zoral\OneDrive\Desktop\ai_project
```

Если файла `.env` ещё нет, создайте его из безопасного шаблона:

```powershell
Copy-Item .env.example .env
notepad .env
```

Для полноценной проверки нужны две строки:

```dotenv
OPENAI_API_KEY=ВАШ_OPENAI_API_KEY
ADMIN_DASHBOARD_TOKEN=ЛОКАЛЬНЫЙ_СЕКРЕТ_НЕ_КОРОЧЕ_32_СИМВОЛОВ
```

Случайный локальный токен можно создать в PowerShell:

```powershell
[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
```

Скопируйте результат после `ADMIN_DASHBOARD_TOKEN=`. Не отправляйте `.env` в
GitHub. Без `OPENAI_API_KEY` переписка с человеком и переключение режима будут
работать, но ИИ не сможет отвечать.

Для локального запуска оставьте:

```dotenv
APP_ENV=development
HOST=127.0.0.1
PORT=8000
AUTO_OPEN_BROWSER=1
COOKIE_SECURE=0
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

### 0.2. Запустить проект

В PowerShell выполните:

```powershell
cmd /c start.bat
```

Если Windows открывает `start.bat` как текстовый файл, не запускайте его двойным
щелчком — используйте команду выше. Запасной вариант:

```powershell
.\.venv\Scripts\python.exe -u run.py
```

Окно PowerShell должно оставаться открытым. Проверка сервера:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

Ожидаемый ответ:

```text
status
------
ok
```

Страницы:

| Назначение | Локальный адрес |
|---|---|
| Пользовательское приложение | `http://127.0.0.1:8000/` |
| Панель менеджера | `http://127.0.0.1:8000/manager` |
| Админ-панель: дашборд и менеджеры | `http://127.0.0.1:8000/admin` |

### 0.3. Проверить работу менеджера

1. Откройте `http://127.0.0.1:8000/`.
2. Войдите анонимно или через настроенный мессенджер.
3. Завершите анкету, если это новый локальный пользователь.
4. Напишите в чат: `Позови человека`.
5. Выберите вариант `Чат`.
6. В соседней вкладке откройте `http://127.0.0.1:8000/admin`, введите
   `ADMIN_DASHBOARD_TOKEN` из `.env`, откройте вкладку «Управление менеджерами»
   и создайте менеджера: задайте имя, логин и пароль не короче 6 символов.
7. Откройте `http://127.0.0.1:8000/manager` и войдите под созданными логином и паролем.
8. Откройте появившееся обращение. Проверьте историю чата и карточку пользователя
   справа.
9. Выключите переключатель `ИИ отвечает`.
10. Вернитесь в пользовательскую вкладку. Не позднее чем через 3 секунды появится
    уведомление `С вами общается менеджер`, а поле ввода изменится на
    `Напишите менеджеру...`.
11. Отправьте сообщение пользователя. ИИ отвечать не должен: сообщение останется
    в ожидании человека.
12. Вернитесь в панель менеджера. Очередь обновляется примерно раз в 5 секунд.
    Напишите ответ — он появится у пользователя примерно за 3 секунды.
13. Снова включите `ИИ отвечает` и задайте новый вопрос от пользователя. При
    настроенном `OPENAI_API_KEY` ответит ИИ с учётом предыдущей истории.

Переключатель действует только на выбранный диалог. Другие пользователи и другие
диалоги продолжают работать в собственном режиме.

### 0.4. Проверить с телефона в одной Wi-Fi сети

В локальном `.env` временно измените:

```dotenv
HOST=0.0.0.0
```

Перезапустите проект и узнайте IPv4-адрес компьютера:

```powershell
ipconfig
```

Если адрес компьютера, например, `192.168.1.25`, откройте на телефоне:

```text
http://192.168.1.25:8000/
http://192.168.1.25:8000/manager
```

Телефон и компьютер должны находиться в одной Wi-Fi сети. Если Windows покажет
запрос брандмауэра, разрешите доступ только для частной сети. После проверки
верните `HOST=127.0.0.1`.

Для остановки локального сервера нажмите `Ctrl+C` в окне PowerShell.

### 0.5. Если локальная страница не открывается

Если порт `8000` занят другим проектом, задайте в `.env`, например:

```dotenv
PORT=8010
PUBLIC_BASE_URL=http://127.0.0.1:8010
```

После перезапуска используйте адреса
`http://127.0.0.1:8010/` и `http://127.0.0.1:8010/manager`.

Дополнительные проверки:

```powershell
Get-Process python -ErrorAction SilentlyContinue
Test-NetConnection 127.0.0.1 -Port 8000
Get-Content server-error.log -Tail 50
```

- `ERR_EMPTY_RESPONSE` обычно означает, что Python завершился с ошибкой:
  посмотрите текст в PowerShell и `server-error.log`;
- сообщение `Неверный логин или пароль` на странице `/manager` означает, что
  учётная запись ещё не создана, отключена или данные введены неверно. Создавать
  и включать менеджеров нужно в `/admin`;
- если после обновления отображается старая версия интерфейса, нажмите
  `Ctrl+F5`;
- одновременно должен работать только один локальный экземпляр проекта на
  выбранном порту.

### 0.6. Проверить установку на рабочий стол

«Консилиум» работает как PWA: отдельная публикация в Google Play и App Store
для этого не нужна. После заполнения анкеты и знакомства с возможностями сервис
сам предложит добавить его на рабочий стол. Позже это действие всегда можно
найти в меню **☰ → На рабочий стол**.

На Android:

1. Откройте публичный HTTPS-адрес в Chrome.
2. Нажмите **Добавить** в предложении сервиса.
3. Подтвердите системное окно **Установить**.

На iPhone или iPad:

1. Откройте публичный HTTPS-адрес именно в Safari.
2. В предложении сервиса нажмите **Понятно**.
3. В Safari нажмите **Поделиться → На экран «Домой» → Добавить**.

После этого появится значок «Консилиума», а сервис будет открываться без
обычной панели браузера. Это всё ещё веб-приложение: обновления появляются
после публикации новой версии на сервере, повторно устанавливать ничего не
нужно.

> Системная установка PWA требует HTTPS. Адреса `127.0.0.1` и `localhost`
> подходят для разработки на компьютере, но для проверки установки на телефоне
> используйте публичный адрес `https://consilium.chelovecbitmax.ru`.

## 1. Создать DNS-запись

В панели управления доменом создайте A-запись:

```text
consilium.chelovecbitmax.ru → IP_АДРЕС_СЕРВЕРА
```

Дождитесь, пока поддомен начнёт определяться:

```bash
getent hosts consilium.chelovecbitmax.ru
```

## 2. Подключиться и проверить действующие проекты

```bash
ssh root@IP_АДРЕС_СЕРВЕРА
export DOCKER_API_VERSION=1.43
```

Сохраните состояние перед установкой:

```bash
mkdir -p /root/backups/consilium-preinstall
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' \
  | tee /root/backups/consilium-preinstall/docker-before.txt
ss -ltnp | tee /root/backups/consilium-preinstall/ports-before.txt
nginx -T > /root/backups/consilium-preinstall/nginx-before.txt 2>&1
nginx -t
```

Проверьте свободный порт:

```bash
ss -ltnp | grep ':8002 '
```

До запуска «Консилиума» команда не должна ничего вывести. Если `8002` занят,
не останавливайте найденный процесс — выберите другой свободный порт и укажите
его одновременно в `.env` и конфигурации Nginx.

## 3. Загрузить проект из GitHub

### 3.1. Создать отдельный репозиторий

```powershell
git clone https://github.com/gogi1704/anamnez_v2.git
```

### 3.2. Проверить состав перед отправкой

```powershell
git status --short --ignored
git ls-files
```

В GitHub нельзя отправлять `.env`, базы, логи, `.venv`, `data`, `backups`,
`dist`, приватные ключи и файлы учётных данных. Файлы с окончанием `.example`
содержат только шаблоны и должны находиться в репозитории.

Добавьте изменения и ещё раз проверьте список:

```powershell
git add .
git status --short
git diff --cached --name-only
```

В списке не должно быть `.env`, файлов баз данных, логов, `.venv`, `data`,
`backups`, `dist`, `.agents`, `.codex`, `.idea` и файлов с ключами.

Если список правильный:

```powershell
git commit -m "Prepare Consilium Docker deployment"
git push -u origin master
```

Не выполняйте `push`, пока `git remote -v` показывает репозиторий другого
проекта.

### 3.3. Клонировать на сервер

На сервере клонируйте проект в отдельный каталог:

```bash
cd /root
git clone ВАША_ССЫЛКА_GITHUB anamnez_v2
cd /root/anamnez_v2
ls -la
```

Для приватного репозитория используйте SSH deploy key или GitHub Personal
Access Token с доступом только на чтение этого репозитория. Не записывайте
GitHub-токен в `.env` приложения и не вставляйте его прямо в команды, которые
останутся в истории shell.

Должны присутствовать `Dockerfile`, `docker-compose.yml`,
`.env.docker.example`, `backend`, `static`, `index.html`, `manager.html`
и `run.py`.

## 4. Создать каталоги данных

Контейнер запускается от пользователя с UID `1000`, как и
`bitrix_connector`:

```bash
cd /root/anamnez_v2
mkdir -p data logs backups
chown -R 1000:1000 data logs backups
chmod -R u+rwX data logs backups
```

База будет храниться в `/root/anamnez_v2/data/consilium.db`. Пересоздание
контейнера её не удалит.

## 5. Создать `.env`

```bash
cd /root/anamnez_v2
cp .env.docker.example .env
nano .env
```

Основные параметры:

```dotenv
APP_ENV=production
OPENAI_API_KEY=ВАШ_OPENAI_API_KEY
ORCHESTRATOR_MODEL=gpt-5.6-luna
SPECIALIST_MODEL=gpt-5.6-sol
DATABASE_PATH=/app/data/consilium.db
ANALYTICS_DATABASE_PATH=/app/data/analytics.db
ANALYTICS_ENABLED=1
ANALYTICS_RETENTION_DAYS=90
YANDEX_METRIKA_COUNTER_ID=
LOG_PATH=/app/logs/server-error.log
MAX_HISTORY_MESSAGES=30
MAX_HISTORY_CHARS=16000

RUNNING_IN_DOCKER=1
HOST=0.0.0.0
PORT=8000
CONSILIUM_HOST_PORT=8002

AUTO_OPEN_BROWSER=0
COOKIE_SECURE=1
PUBLIC_BASE_URL=https://consilium.chelovecbitmax.ru
BOT_INTEGRATION_SECRET=ДЛИННЫЙ_СЛУЧАЙНЫЙ_СЕКРЕТ
ADMIN_DASHBOARD_TOKEN=ДРУГОЙ_ДЛИННЫЙ_СЛУЧАЙНЫЙ_СЕКРЕТ
AUTH_LINK_TTL_SECONDS=604800
AUTH_INTENT_TTL_SECONDS=604800
TELEGRAM_BOT_AUTH_URL=
MAX_BOT_AUTH_URL=
SESSION_TTL_DAYS=90
SESSION_COOKIE_NAME=consilium_session

AFTER_TESTS_GOOGLE_CREDENTIALS_HOST=/root/anketa_bot_max_web/docs/after-tests-db-e0cd34372c4a.json
LAB_RESULTS_ENABLED=1
AFTER_TESTS_GOOGLE_CREDENTIALS=/run/secrets/after-tests-google.json
AFTER_TESTS_SPREADSHEET=after_tests_db
AFTER_TESTS_WORKSHEET=tetst_and_results
GOOGLE_SHEETS_TIMEOUT_SECONDS=15
LAB_RESULTS_CACHE_SECONDS=60
```

Создайте два разных секрета для «Консилиума»:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Первый результат вставьте в `BOT_INTEGRATION_SECRET`, второй — в
`ADMIN_DASHBOARD_TOKEN`. Не используйте один секрет для двух назначений и не
копируйте токены `anketa_bot_max` или `bitrix_connector`.

`AUTH_LINK_TTL_SECONDS` — срок, за который готовую ссылку входа нужно
активировать первый раз. После активации та же ссылка может повторно открыть
«Консилиум» только в браузере с действующей сессией её владельца; в чужом
браузере она покажет вход через мессенджер и не даст доступ к данным.
`AUTH_INTENT_TTL_SECONDS` — срок одноразового запроса, который приложение
передаёт боту для привязки уже заполненной анкеты. Значение `604800` равно семи
дням. Запрос привязки перестаёт работать после первого использования, даже если
срок ещё не истёк.

Если бот ещё не создан, оставьте соответствующий `*_BOT_AUTH_URL` пустым.
Для работающих ботов укажите обе стартовые ссылки с обязательным `{token}`:

```dotenv
TELEGRAM_BOT_AUTH_URL=https://t.me/имя_бота?start={token}
MAX_BOT_AUTH_URL=https://max.ru/имя_бота?start={token}
```

Проект `max_to_consilium` уже реализует получение MAX ID, обработку параметра
`start` и выдачу одноразовой ссылки. После изменения `.env` выполните:

```bash
cd /root/anamnez_v2
docker-compose up -d --force-recreate consilium
```

### Контракт Telegram- и MAX-ботов

Приложение уже создаёт запрос привязки при нажатии кнопки мессенджера. Бот
получает значение `{token}` в команде старта. После проверки пользователя самим
мессенджером бот вызывает:

```http
POST https://consilium.chelovecbitmax.ru/api/auth/messenger/link
Authorization: Bearer ЗНАЧЕНИЕ_BOT_INTEGRATION_SECRET
Content-Type: application/json

{
  "provider": "telegram",
  "provider_user_id": "123456789",
  "intent_token": "ТОКЕН_ИЗ_КОМАНДЫ_СТАРТ"
}
```

Для MAX передавайте `"provider": "max"` и настоящий MAX ID. Значение
`provider_user_id` нужно брать только из подписанного события/SDK мессенджера,
а не из текста, присланного пользователем. Ответ содержит `auth_url`; именно
эту одноразовую ссылку бот отправляет пользователю:

```json
{
  "auth_url": "https://consilium.chelovecbitmax.ru/auth/messenger?t=...",
  "expires_at": "2026-08-05T12:00:00+00:00",
  "chel_id": "chel_..."
}
```

При первом подтверждении внешний ID записывается в `external_identities` и
привязывается к текущему `chel_id`, поэтому уже заполненная анкета не теряется.
При входе с другого устройства тот же `provider_user_id` возвращает исходный
`chel_id`. Один пользователь может иметь одновременно Telegram и MAX.

Старый маршрут `POST /api/auth/max/link` и ссылка `/auth/max` оставлены для
совместимости с существующей интеграцией MAX.

MAX-бот разворачивается отдельным контейнером и подключается к уже созданной
сети Консилиума:

```bash
cd /root/max_to_consilium
cp .env.example .env
nano .env
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 consilium-max-bot
```

В `.env` MAX-бота используйте `CONSILIUM_API_URL=http://consilium:8000` и то же
значение `BOT_INTEGRATION_SECRET`, что у Консилиума. Порт наружу этому
контейнеру не открывается; существующие проекты и их порты не изменяются.

Админ-панель открывается по адресу
`https://consilium.chelovecbitmax.ru/admin` (старый адрес `/dashboard` также
работает). Введите в ней значение `ADMIN_DASHBOARD_TOKEN`. Токен хранится только
до закрытия вкладки. В разделе «Дашборд» доступны показатели проекта без текстов
медицинских сообщений. В разделе «Управление менеджерами» администратор создаёт
личные учётные записи, меняет имена и пароли, включает и отключает доступ либо
полностью удаляет менеджера. При удалении история уже отправленных ответов остаётся.
В разделе «Дополнительные обследования» администратор управляет каталогом,
который пользователь видит после анкеты: добавляет и удаляет позиции, меняет
названия, описания, состав и цены. Изменения начинают действовать сразу.
Дашборд также показывает распределение аудитории по типам устройств: ПК,
Android, iOS и другим, а отдельные диаграммы — по операционным системам и
браузерам. В таблице «Устройства» можно искать по `chel_id`,
операционной системе и браузеру, видеть первый и последний вход и число открытий
приложения. Статистика обновляется при открытии главной страницы. IP-адреса не
сохраняются; поисковые роботы в аудиторию не включаются.

Вкладка «Воронка и поведение» показывает путь пользователя от регистрации до
первого сообщения и обращения к человеку. Отчёт можно фильтровать по периоду,
устройству, способу регистрации и источнику перехода. Отдельно показываются
отказы между этапами, прохождение каждого вопроса анкеты, среднее время ответа,
возвраты назад, ошибки валидации, выбор обследований и технические ошибки.
Если пользователь пришёл через splitter по ссылке вида
`/go?source=manager_1`, splitter передаёт в консилиум
`splitter_source=manager_1`. Эта метка сохраняется в поле пользователя
`from_manager` до завершения регистрации и после регистрации уже не меняется.
Параметр `splitter_attempt` связывает один редирект с подтверждениями доставки.
Консилиум отправляет в сплиттер этапы достижения сервера, запуска JavaScript и
фактического показа приветственного экрана. Для этого в `.env` задайте
`SPLITTER_EVENT_URL=http://ab_splitter:8000/api/delivery/event`, а значение
`SPLITTER_EVENT_SECRET` сделайте равным `CONVERSION_SECRET` сплиттера. Сбой
этой служебной отправки не блокирует загрузку Консилиума.
В аналитике показываются количество пользователей каждого менеджера, доля от
всех зарегистрированных пользователей, число выбравших дополнительные
обследования и конверсия в выбор. Для создания ссылки непосредственно ботом
интеграционный запрос `/api/auth/messenger` может передать необязательное поле
`from_manager` с той же меткой.
События записываются в отдельную базу `analytics.db` асинхронной очередью в
браузере. Сбой аналитики не блокирует анкету или чат. Тексты ответов, медицинские
данные, телефоны и номера пробирок в эту базу не записываются. По умолчанию
детальные события хранятся 90 дней; срок задаёт `ANALYTICS_RETENTION_DAYS`.

### Яндекс Метрика

В пользовательскую часть встроена ограниченная интеграция с Яндекс Метрикой.
Она отключена, пока в `.env` не указан числовой номер счётчика:

```dotenv
YANDEX_METRIKA_COUNTER_ID=12345678
```

Создайте счётчик для домена `consilium.chelovecbitmax.ru` в Яндекс Метрике и
скопируйте только его номер. Для карты скроллинга включите Вебвизор в интерфейсе
счётчика, но отключите опцию записи всех полей. Приложение дополнительно
маскирует поля, формы и чувствительные медицинские блоки в самом HTML.
Рекомендуется включить маскирование IP и принимать данные только с указанных
адресов сайта. После изменения `.env` пересоздайте контейнер проекта.

Метрика загружается без отдельного окна согласия. Отправляется безопасный просмотр
страницы без query-параметров, поэтому одноразовые токены и UTM-параметры в URL
не передаются. Включены карта кликов и отслеживание внешних ссылок. Дополнительно
передаются вручную заданные технические цели: продолжение приветствия,
выбор способа регистрации, начало и завершение анкеты, просмотр и выбор
обследований, выбор оплаты, открытие чата, первое сообщение, запрос человека и
установка приложения. Параметры целей не отправляются. `chel_id`, имя, ИНН,
телефон, ответы анкеты, симптомы, сообщения и результаты анализов не передаются.
Счётчик не подключается на страницах `/admin` и `/manager`.

Для работы карты скроллинга Вебвизор включён в защищённом режиме. Все поля ввода
помечаются `ym-disable-keys`, формы — `ym-disable-submit`, а медицинская анкета,
чат, профиль, симптомы и результаты анализов — `ym-hide-content`. Метрика
получает координаты действий и глубину скролла, но не содержимое этих блоков.

Для просмотра карт главная пользовательская страница разрешает встраивание во
фрейм только доменам Яндекс Метрики и Webvisor. `/admin`, `/manager` и API
по-прежнему возвращают `X-Frame-Options: DENY` и не могут быть встроены во фрейм.

Создайте в интерфейсе Метрики JavaScript-цели с идентификаторами:

```text
welcome_continue
registration_max
registration_telegram
registration_anonymous
font_size_selected
questionnaire_started
questionnaire_completed
exam_offer_viewed
exam_options_opened
exam_selection_completed
payment_online
payment_at_exam
onboarding_completed
capabilities_viewed
chat_opened
first_message_sent
human_requested
install_clicked
```

Проверить загрузку можно, открыв сайт с параметром `?_ym_debug=2` и выполнив
одну из целей. Не добавляйте в URL, UTM-метки или названия целей персональные и
медицинские сведения.

### Подсказки организаций по ИНН

В первом вопросе анкеты и в разделе «Мои данные» можно показывать название
организации по мере ввода ИНН. Подсказки начинаются после четырёх цифр. Для
работы нужен API-ключ сервиса DaData; добавьте его только в рабочий `.env`:

```dotenv
DADATA_API_KEY=ваш_серверный_api_ключ
DADATA_SUGGESTIONS_URL=https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party
DADATA_TIMEOUT_SECONDS=5
DADATA_SUGGESTIONS_CACHE_SECONDS=600
```

Ключ остаётся на сервере и не передаётся в браузер. Частичный ИНН отправляется
на сервер POST-запросом, поэтому не попадает в URL обычного журнала доступа.
Если ключ не задан или сервис подсказок временно недоступен, анкета не
блокируется: пользователь вводит ИНН вручную. После изменения `.env`
пересоберите контейнер Консилиума.

ИИ-менеджер получает обезличенный тип текущего устройства, ОС и браузер и может
объяснить, как добавить Консилиум на рабочий стол или экран «Домой». Полный
`User-Agent` в запрос модели не передаётся.

Рабочее место менеджера открывается отдельно:
`https://consilium.chelovecbitmax.ru/manager`. Для входа используются личные
логин и пароль, созданные администратором. Секрет `ADMIN_DASHBOARD_TOKEN`
менеджерам передавать не нужно. Панель показывает чувствительные
данные, поэтому открывайте её только на доверенном устройстве и не передавайте
пароль в переписке.

В панели менеджера:

- слева находится очередь обращений и чатов, в которых выключен ИИ;
- в центре видна вся переписка пользователя с ИИ и человеком;
- справа доступны анкета, симптомы, номер пробирки, документы анализов,
  сохранённые расшифровки и память пользователя;
- переключатель «ИИ отвечает» действует только на выбранный диалог;
- если ИИ выключен, новые сообщения пользователя сохраняются без
  автоматического ответа и ожидают менеджера;
- ответ менеджера попадает в ту же историю и появляется у пользователя
  автоматически;
- менеджер может закрыть обращение: оно исчезнет из открытой очереди, а ИИ снова
  сможет отвечать пользователю;
- панель менеджера подаёт разные короткие сигналы для нового обращения и для
  сообщения, ожидающего человека при выключенном ИИ; пользователь слышит сигнал
  только при получении нового ответа;
- все ответы менеджеров и переключения режима записываются в журнал действий.

При совместной работе создайте каждому менеджеру отдельную учётную запись:
имя вошедшего сотрудника сохраняется рядом с ответами и в журнале. Не используйте
общую учётную запись.

Ограничьте права:

```bash
chmod 600 /root/anamnez_v2/.env
```

Проверьте существующий ключ Google до запуска контейнера:

```bash
test -f /root/anketa_bot_max_web/docs/after-tests-db-e0cd34372c4a.json
ls -l /root/anketa_bot_max_web/docs/after-tests-db-e0cd34372c4a.json
```

Docker подключает только этот файл в
`/run/secrets/after-tests-google.json` с флагом `:ro`. Не копируйте JSON-ключ
в репозиторий «Консилиума» и не добавляйте его содержимое в `.env`.

Приложение поддерживает оба встречающихся имени листа:
`tetst_and_results` и `tests_and_results`.

В найденном результате пользователь может открыть каждый документ, отправить
его в чат и запросить расшифровку одного документа или всего набора. Расшифровка
выполняется только после явного запроса пользователя. Для неё документ по ссылке
передаётся в OpenAI API, поэтому ссылка должна открывать сам файл без
интерактивного входа в Google. Расшифровка сохраняется в SQLite и повторно
используется, пока не изменились анкета пользователя или список документов.

## 6. Проверить Docker Compose

На текущем сервере используется Docker Compose v1, поэтому команды пишутся
через дефис:

```bash
docker network inspect consilium-internal >/dev/null 2>&1 \
  || docker network create consilium-internal
cd /root/anamnez_v2
docker-compose version
docker-compose config
```

В выводе `docker-compose config` проверьте:

- имя сервиса `consilium`;
- контейнер `consilium`;
- публикацию `127.0.0.1:8002:8000`;
- отдельную сеть `consilium-internal`;
- отдельные каталоги `/root/anamnez_v2/data`, `logs` и `backups`.

Не публикуйте порт как `0.0.0.0:8002:8000` и не добавляйте `8002` в firewall.

## 7. Собрать образ

```bash
cd /root/anamnez_v2
docker-compose build
```

Эта команда собирает только образ нового проекта и не пересобирает
`anketa_bot_max` или `bitrix_connector`.

## 8. Выполнить production-проверку

```bash
cd /root/anamnez_v2
docker-compose run --rm consilium python scripts/production_check.py
```

Продолжайте только если в итоговой строке указано `0 ошибок`.

После проверки ещё раз исправьте права, если Docker создал файлы:

```bash
chown -R 1000:1000 /root/anamnez_v2/data /root/anamnez_v2/logs /root/anamnez_v2/backups
```

## 9. Запустить контейнер

```bash
cd /root/anamnez_v2
docker-compose up -d
docker-compose ps
docker-compose logs --tail=100 consilium
```

Проверьте приложение непосредственно на сервере:

```bash
curl -i http://127.0.0.1:8002/api/health
curl -i http://127.0.0.1:8002/api/ready
```

Ожидается HTTP 200. В `docker-compose ps` контейнер должен получить состояние
`Up (healthy)` после завершения healthcheck.

## 10. Добавить отдельную конфигурацию Nginx

```bash
cp /root/anamnez_v2/deploy/nginx-consilium.conf \
  /etc/nginx/sites-available/consilium
nano /etc/nginx/sites-available/consilium
```

```bash
limit_req_zone $binary_remote_addr zone=consilium_ai:10m rate=12r/m;

server {
    listen 80;
    listen [::]:80;
    server_name consilium.chelovecbitmax.ru;

    client_max_body_size 18m;
    limit_req_status 429;
    server_tokens off;

    location = /auth/max {
        access_log off;
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_buffering off;
    }

    location ~ ^/api/(chat|council|second-opinion|lab-results(?:/interpret)?)$ {
        limit_req zone=consilium_ai burst=4 nodelay;
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
        proxy_buffering off;
    }

    # Админка защищена отдельным токеном. Не применяйте к ней лимит AI-запросов:
    # один экран загружает несколько независимых таблиц.
    location ^~ /api/admin/ {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
        proxy_buffering off;
    }
}

```

Проверьте две настройки:

```nginx
server_name consilium.chelovecbitmax.ru;
proxy_pass http://127.0.0.1:8002;
```

Не редактируйте существующие файлы Nginx для `anketa_bot_max` и
`bitrix_connector`.

Активируйте новый сайт:

```bash
test -L /etc/nginx/sites-enabled/consilium || \
  ln -s /etc/nginx/sites-available/consilium \
    /etc/nginx/sites-enabled/consilium
nginx -t
```

Сообщение `File exists` у старой команды не является ошибкой конфигурации:
оно означает, что сайт уже активирован. Главное — чтобы `nginx -t` завершился
строкой `test is successful`.

Если `nginx -t` показывает ошибку, не перезагружайте Nginx. Удалите только
новую ссылку и проверьте старые сайты:

```bash
rm -f /etc/nginx/sites-enabled/consilium
nginx -t
```

Если проверка успешна:

```bash
systemctl reload nginx
curl -I -H 'Host: consilium.chelovecbitmax.ru' http://127.0.0.1/
```

`reload` не останавливает работающие соединения Nginx.

## 11. Подключить HTTPS

Когда DNS уже указывает на сервер:

```bash
certbot --nginx -d consilium.chelovecbitmax.ru
nginx -t
systemctl reload nginx
```

Проверка:

```bash
curl -I https://consilium.chelovecbitmax.ru/
curl https://consilium.chelovecbitmax.ru/api/ready
```

## 12. Проверить, что старые проекты работают

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
ss -ltnp | grep -E ':8000 |:8001 |:8002 '
nginx -t
curl http://127.0.0.1:8001/health
curl https://chelovecbitmax.ru/health
```

## Онлайн-оплата через ЮKassa

Консилиум использует сценарий Redirect. Сервер фиксирует выбранные
обследования, рассчитывает сумму по каталогу в базе, создаёт платёж в ЮKassa и
перенаправляет пользователя на защищённую платёжную страницу. Данные банковской
карты через Консилиум не проходят и в его базе не сохраняются.

Добавьте в рабочий `.env` реквизиты тестового или боевого магазина:

```dotenv
ONLINE_PAYMENTS_ENABLED=true
YOOKASSA_SHOP_ID=идентификатор_магазина
YOOKASSA_SECRET_KEY=секретный_ключ
YOOKASSA_API_URL=https://api.yookassa.ru/v3
YOOKASSA_TIMEOUT_SECONDS=20
```

При `ONLINE_PAYMENTS_ENABLED=true` кнопка «Оплатить онлайн» создаёт заказ и
перенаправляет пользователя на защищённую страницу ЮKassa. Чтобы временно
вернуть информационную заглушку без удаления кода интеграции, установите
`ONLINE_PAYMENTS_ENABLED=false` и пересоберите контейнер Консилиума.

Секретный ключ нельзя добавлять в Git. Рабочий `.env` исключён через
`.gitignore` и `.dockerignore`.

В личном кабинете ЮKassa добавьте URL входящих уведомлений:

```text
https://consilium.chelovecbitmax.ru/api/payments/yookassa/webhook
```

Подпишитесь на `payment.succeeded`, `payment.canceled` и
`payment.waiting_for_capture`. Сервер повторно получает платёж из API ЮKassa,
сверяет ID заказа, валюту и сумму и только после статуса `succeeded` отмечает
заказ оплаченным.

### Уведомления об оплате в Битрикс24

Уведомление передаётся во внутренний `bitrix_connector` только после повторной
проверки платежа через API ЮKassa. Коннектор сохраняет задание в своей SQLite-
очереди, повторяет временно неуспешную отправку и устраняет дубли по локальному
номеру заказа. В `.env` Консилиума укажите:

```dotenv
BITRIX_CONNECTOR_URL=http://bitrix_connector:8000
BITRIX_PAYMENT_SECRET=одинаковая-случайная-строка-не-короче-32-символов
BITRIX_CONNECTOR_TIMEOUT_SECONDS=5
```

В `.env` проекта `bitrix_connector` значение `CONSILIUM_PAYMENT_SECRET` должно
совпадать с `BITRIX_PAYMENT_SECRET`, а `BITRIX_PAYMENT_DIALOG_ID` должен
содержать адрес чата (`chat123`), чата группы/проекта (`sg123`) или ID
пользователя. Оба контейнера подключаются к внешней Docker-сети
`consilium-internal`. Банковские реквизиты в сообщение не передаются.

Чтобы уведомление дополнительно содержало бригаду и дату медосмотра,
Консилиум раз в сутки синхронизирует read-only график предприятий. В `.env`
Консилиума укажите токены отдельной учётной записи сервиса графика:

```dotenv
EXAMINATION_SCHEDULE_ENABLED=true
EXAMINATION_SCHEDULE_API_URL=https://api.chelovekgrafik.ru/api
EXAMINATION_SCHEDULE_ACCESS_TOKEN=секретный-access-token
EXAMINATION_SCHEDULE_REFRESH_TOKEN=секретный-refresh-token
EXAMINATION_SCHEDULE_TOKEN_CACHE_PATH=/app/data/examination-schedule-auth.json
EXAMINATION_SCHEDULE_SYNC_INTERVAL_SECONDS=86400
```

Токены нельзя добавлять в Git или передавать в браузерный JavaScript
Консилиума. При запуске первая синхронизация выполняется через несколько
секунд, затем — раз в сутки. Загружаются осмотры от текущей даты до той же даты
через два месяца. Записи сопоставляются с профилем плательщика по ИНН. Для
уведомления выбирается ближайшая предстоящая дата; если на неё назначено
несколько бригад, в Bitrix перечисляются все. Отсутствие совпадения или
временная недоступность графика не блокирует подтверждение платежа.
Обновлённые сервисом токены сохраняются только в закрытом файле внутри
подключённого каталога `data`, поэтому продолжают работать после перезапуска.

Если подключено решение для чеков по 54-ФЗ, сначала согласуйте с бухгалтером
ставку НДС и способ расчёта, затем включите передачу чека:

```dotenv
YOOKASSA_RECEIPTS_ENABLED=1
YOOKASSA_VAT_CODE=1
YOOKASSA_PAYMENT_MODE=full_prepayment
```

При включённых чеках интерфейс запросит электронную почту. После изменения
`.env` пересоберите только контейнер Консилиума:

```bash
cd /root/anamnez_v2
docker-compose up -d --build consilium
docker-compose logs --tail=100 consilium
```

Сначала используйте тестовый магазин. Возврат пользователя на сайт сам по себе
не означает успех: завершение показывается только после серверной проверки.

Каждая попытка оплаты сохраняется в базе и доступна пользователю в разделе
«Мои покупки» в меню чата: дата и время, состав заказа, сумма и состояние
платежа. После подтверждения `payment.succeeded` приложение показывает экран
успеха с инструкцией и кнопками «Открыть мои покупки» и «Перейти в чат».

В админ-панели раздел аналитики показывает количество попыток и плательщиков,
конверсию в успешную оплату, подтверждённую выручку, распределение статусов,
популярность оплаченных обследований и последние транзакции. Тестовые платежи
отмечаются отдельно и не входят в фактическую выручку. В «Метрике 2.0» экраны
проверки, успешной и незавершённой оплаты считаются отдельными этапами пути.

В истории пользователь может продолжить активный Redirect-платёж, вручную
обновить его состояние или повторить неуспешный заказ. Успешные и активные
операции удалить нельзя. Отменённые, незавершённые и ошибочные попытки можно
скрыть после подтверждения; физически они сохраняются в базе для аудита. Если
скрытая незавершённая попытка позднее получит `payment.succeeded`, она снова
появится в истории как оплаченная. Повторный запуск того же незавершённого
заказа использует прежний ключ идемпотентности и не создаёт второе списание.

Если пользователь нажал выход на странице ЮKassa и вернулся в Консилиум,
сервер сначала повторно проверяет платёж. Успешное списание никогда не
отменяется локально. Платёж со статусом `waiting_for_capture` отменяется через
API ЮKassa, а незавершённый одностадийный платёж `pending` отмечается в
Консилиуме как «Не завершено». Если ЮKassa позднее подтвердит такой платёж,
входящее уведомление заменит локальный статус на успешный. Простое закрытие
вкладки браузера не может отправить серверу команду; в этом случае окончательное
состояние придёт через webhook или после возврата пользователя на сайт.

Ожидаемая схема:

| Проект | Порт хоста | Состояние |
|---|---:|---|
| `anketa_bot_max` | `127.0.0.1:8000` | без изменений |
| `bitrix_connector` | `127.0.0.1:8001` | без изменений |
| `consilium` | `127.0.0.1:8002` | новый контейнер |

## 13. Полезные команды

Логи:

```bash
cd /root/anamnez_v2
docker-compose logs -f consilium
```

Перезапуск:

```bash
cd /root/anamnez_v2
docker-compose restart consilium
```

Проверка:

```bash
cd /root/anamnez_v2
docker-compose ps
curl http://127.0.0.1:8002/api/ready
```

Остановка только «Консилиума»:

```bash
cd /root/anamnez_v2
docker-compose stop consilium
```

Команды выполняются из `/root/anamnez_v2`, поэтому они не управляют Compose-
проектом `bitrix_connector`.

## 14. Резервная копия базы

Приложение умеет делать согласованную SQLite-копию без остановки:

```bash
cd /root/anamnez_v2
docker-compose exec -T consilium \
  python scripts/backup_database.py --destination /app/backups --keep 14
ls -lah /root/anamnez_v2/backups
```

Также сохраняйте отдельно:

```text
/root/anamnez_v2/.env
/root/anamnez_v2/data/consilium.db
/root/anamnez_v2/backups/
```

## 15. Обновление

Перед обновлением:

```bash
cd /root/anamnez_v2
docker-compose exec -T consilium \
  python scripts/backup_database.py --destination /app/backups --keep 14
cp -a /root/anamnez_v2 /root/anamnez_v2-rollback
```

При обновлении версии, в которой впервые подключаются результаты анализов,
откройте существующий `/root/anamnez_v2/.env` и добавьте параметры
`AFTER_TESTS_*`, `LAB_RESULTS_*` и `GOOGLE_SHEETS_TIMEOUT_SECONDS` из пункта 5.
Файл `.env` команда `git pull` не изменяет.

При первом обновлении до версии с дашбордом также добавьте в существующий `.env`
отдельный `ADMIN_DASHBOARD_TOKEN` из пункта 5. Без него страница откроется, но
API админ-панели останется отключённым. После запуска откройте `/admin`,
перейдите в «Управление менеджерами» и создайте каждому сотруднику личные логин
и пароль. Таблицы учётных записей добавятся в существующую базу автоматически;
анкеты, диалоги и обращения пользователей не очищаются.

В таблице «Пользователи» отображаются общее число пользователей, число новых
регистраций за выбранный период и количество записей, найденных с учётом всех
фильтров. Доступны периоды «сегодня», 7, 30 и 90 дней, а также произвольные даты.

При первом обновлении до версии с экраном идентификации добавьте:

```dotenv
AUTH_INTENT_TTL_SECONDS=604800
TELEGRAM_BOT_AUTH_URL=
MAX_BOT_AUTH_URL=
```

Пустые ссылки безопасны: приложение продолжит работать, а кнопки Telegram и MAX
будут сообщать, что соответствующий бот ещё не подключён.

Получите изменения из GitHub и пересоберите только этот проект:

```bash
cd /root/anamnez_v2
git status --short
git pull --ff-only
docker-compose build
docker-compose run --rm consilium python scripts/production_check.py
docker-compose up -d
docker-compose ps
curl http://127.0.0.1:8002/api/ready
```

Обновите отдельный Nginx-файл, чтобы применить маршруты авторизации, результатов
анализов и административной аналитики:

Для версии с исправлением регулярной ошибки `429` в админ-панели этот шаг
обязателен: конфигурация разделяет лимиты AI-запросов, входа и авторизованной
админки.

```bash
cp /root/anamnez_v2/deploy/nginx-consilium.conf \
  /etc/nginx/sites-available/consilium
nginx -t
systemctl reload nginx
```

Не используйте `docker-compose down -v`: параметр `-v` может удалить
постоянные данные.

## 16. Запустить Telegram- и MAX-ботов отдельными контейнерами

Каждый бот хранится в отдельном репозитории и отдельном каталоге. Они не
публикуют порты хоста и связываются с Консилиумом только через внешнюю сеть
`consilium-internal`.

Клонируйте проекты, не затрагивая существующие каталоги:

```bash
cd /root
git clone ССЫЛКА_НА_TELEGRAM_РЕПОЗИТОРИЙ tg_to_consillium
git clone ССЫЛКА_НА_MAX_РЕПОЗИТОРИЙ max_to_consilium
```

Подготовьте настройки Telegram-бота:

```bash
cd /root/tg_to_consillium
cp .env.production.example .env
nano .env
```

Подготовьте настройки MAX-бота:

```bash
cd /root/max_to_consilium
cp .env.production.example .env
nano .env
```

В обоих файлах `.env` значение `BOT_INTEGRATION_SECRET` должно посимвольно
совпадать с `/root/anamnez_v2/.env`. Внутренний адрес должен быть одинаковым:

```dotenv
CONSILIUM_API_URL=http://consilium:8000
```

Перед запуском выполните общую проверку. Она не печатает значения секретов:

```bash
python3 /root/anamnez_v2/scripts/deployment_bundle_check.py \
  --consilium-dir /root/anamnez_v2 \
  --telegram-dir /root/tg_to_consillium \
  --max-dir /root/max_to_consilium \
  --check-env
```

Продолжайте только при результате `0 ошибок`. Затем проверьте Compose-файлы и
запустите контейнеры в безопасном порядке:

```bash
docker network inspect consilium-internal >/dev/null 2>&1 \
  || docker network create consilium-internal

cd /root/anamnez_v2
docker-compose up -d --build
curl --fail http://127.0.0.1:8002/api/ready

cd /root/tg_to_consillium
docker-compose config
docker-compose up -d --build

cd /root/max_to_consilium
docker-compose config
docker-compose up -d --build

docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Ожидаемые контейнеры: `consilium`, `consilium-telegram-bot` и
`consilium-max-bot`. Только `consilium` должен показывать публикацию
`127.0.0.1:8002->8000/tcp`; у ботов публикации портов быть не должно.

Проверьте журналы без вывода `.env`:

```bash
docker-compose -f /root/tg_to_consillium/docker-compose.yml \
  logs --tail=100 consilium-telegram-bot
docker-compose -f /root/max_to_consilium/docker-compose.yml \
  logs --tail=100 consilium-max-bot
```

После этого нажмите «Начать» в каждом боте и проверьте вход в один и тот же
профиль с сохранением `chel_id`. Оба бота перед запуском long polling сами
отключают старый webhook без удаления ожидающих сообщений.

Для обновления пересобирайте только изменившийся проект из его каталога. Не
используйте общий `docker-compose down` и не добавляйте сервисы ботов в Compose
файлы `anketa_bot_max` или `bitrix_connector`.

## 17. Уведомления менеджеров в Telegram и MAX

В админ-панели откройте «Управление менеджерами». При создании менеджера можно
сразу указать его Telegram ID и MAX ID. Менеджер должен предварительно нажать
«Старт» в соответствующем боте, иначе мессенджер не разрешит боту отправлять ему
сообщения.

Более безопасный вариант — создать менеджера без ID, а затем нажать
«Привязать Telegram» или «Привязать MAX». Скопируйте полученную одноразовую
ссылку и отправьте её менеджеру. Ссылка действует 7 дней и после первого
использования становится недействительной. Обычный запуск бота продолжает
регистрировать пользователя; служебная ссылка привязывает только учётную запись
менеджера и не создаёт пользовательский профиль.

Уведомления отправляются:

- при появлении нового обращения к человеку;
- при новом сообщении пользователя, если ИИ для этого диалога выключен.

Кнопка в уведомлении открывает нужный диалог на странице `/manager`. Для работы
должны совпадать `BOT_INTEGRATION_SECRET` во всех трёх проектах, а в `.env`
Консилиума должны быть корректно заполнены `TELEGRAM_BOT_AUTH_URL` и
`MAX_BOT_AUTH_URL` с обязательной подстановкой `{token}`.

После публикации обновления пересоберите только три связанных контейнера из их
собственных каталогов:

```bash
cd /root/anamnez_v2 && docker-compose up -d --build consilium
cd /root/tg_to_consillium && docker-compose up -d --build
cd /root/max_to_consilium && docker-compose up -d --build
```

Проверка:

```bash
docker-compose ps
docker-compose logs --tail=100 consilium
```

Затем создайте тестового менеджера, привяжите один из мессенджеров, создайте
обращение от тестового пользователя и убедитесь, что кнопка в уведомлении
открывает соответствующий диалог.

## 18. Откат

Если новый проект мешает публикации, отключите только его:

```bash
cd /root/anamnez_v2
docker-compose stop consilium
rm -f /etc/nginx/sites-enabled/consilium
nginx -t
systemctl reload nginx
```

После этого проверьте старые проекты:

```bash
docker ps
curl http://127.0.0.1:8001/health
curl https://chelovecbitmax.ru/health
```

Не выполняйте `docker stop` или `docker rm` без имени контейнера. Не запускайте
`docker-compose down` из каталогов других проектов.
