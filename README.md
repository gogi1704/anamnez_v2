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

> Текущая версия позволяет открыть приложение по обычной ссылке без входа через
> MAX. Это подходит для демонстрации. До использования реальных медицинских
> данных необходимо отдельно включить обязательную авторизацию.

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
`.env.docker.example`, `backend`, `static`, `index.html` и `run.py`.

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
LOG_PATH=/app/logs/server-error.log
MAX_HISTORY_MESSAGES=30

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

Дашборд открывается по адресу
`https://consilium.chelovecbitmax.ru/dashboard`. Введите в нём значение
`ADMIN_DASHBOARD_TOKEN`. Токен хранится только до закрытия вкладки. Дашборд не
показывает тексты медицинских сообщений и содержимое медицинских анкет.

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

## 6. Проверить Docker Compose

На текущем сервере используется Docker Compose v1, поэтому команды пишутся
через дефис:

```bash
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

    location ~ ^/api/(chat|council|second-opinion|lab-results)$ {
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

    location = /api/admin/dashboard {
        limit_req zone=consilium_ai burst=4 nodelay;
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
API аналитики останется отключённым.

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

Обновите отдельный Nginx-файл, чтобы маршруты `lab-results` и административной
аналитики получили ограничение частоты запросов:

```bash
cp /root/anamnez_v2/deploy/nginx-consilium.conf \
  /etc/nginx/sites-available/consilium
nginx -t
systemctl reload nginx
```

Не используйте `docker-compose down -v`: параметр `-v` может удалить
постоянные данные.

## 16. Откат

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
