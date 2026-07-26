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

В GitHub создайте новый приватный репозиторий, например `consilium`. Не
добавляйте при создании README, `.gitignore` или лицензию: эти файлы уже есть
локально.

Текущий `origin` обязательно проверьте перед первой отправкой:

```powershell
git remote -v
```

Он должен указывать именно на новый репозиторий «Консилиума», а не на
`anketa_bot_max`, `bitrix_connector`, `anamnez_v2` или другой проект.

Если `origin` уже существует, замените его:

```powershell
git remote set-url origin https://github.com/ВАШ_ЛОГИН/consilium.git
```

Если `origin` отсутствует:

```powershell
git remote add origin https://github.com/ВАШ_ЛОГИН/consilium.git
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
git clone ВАША_ССЫЛКА_GITHUB consilium
cd /root/consilium
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
cd /root/consilium
mkdir -p data logs backups
chown -R 1000:1000 data logs backups
chmod -R u+rwX data logs backups
```

База будет храниться в `/root/consilium/data/consilium.db`. Пересоздание
контейнера её не удалит.

## 5. Создать `.env`

```bash
cd /root/consilium
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
AUTH_LINK_TTL_SECONDS=604800
SESSION_TTL_DAYS=90
SESSION_COOKIE_NAME=consilium_session
```

Создайте отдельный секрет для «Консилиума»:

```bash
openssl rand -hex 32
```

Вставьте результат в `BOT_INTEGRATION_SECRET`. Не используйте токены или
секреты `anketa_bot_max` и `bitrix_connector`.

Ограничьте права:

```bash
chmod 600 /root/consilium/.env
```

## 6. Проверить Docker Compose

На текущем сервере используется Docker Compose v1, поэтому команды пишутся
через дефис:

```bash
cd /root/consilium
docker-compose version
docker-compose config
```

В выводе `docker-compose config` проверьте:

- имя сервиса `consilium`;
- контейнер `consilium`;
- публикацию `127.0.0.1:8002:8000`;
- отдельную сеть `consilium-internal`;
- отдельные каталоги `/root/consilium/data`, `logs` и `backups`.

Не публикуйте порт как `0.0.0.0:8002:8000` и не добавляйте `8002` в firewall.

## 7. Собрать образ

```bash
cd /root/consilium
docker-compose build
```

Эта команда собирает только образ нового проекта и не пересобирает
`anketa_bot_max` или `bitrix_connector`.

## 8. Выполнить production-проверку

```bash
cd /root/consilium
docker-compose run --rm consilium python scripts/production_check.py
```

Продолжайте только если в итоговой строке указано `0 ошибок`.

После проверки ещё раз исправьте права, если Docker создал файлы:

```bash
chown -R 1000:1000 /root/consilium/data /root/consilium/logs /root/consilium/backups
```

## 9. Запустить контейнер

```bash
cd /root/consilium
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
cp /root/consilium/deploy/nginx-consilium.conf \
  /etc/nginx/sites-available/consilium
nano /etc/nginx/sites-available/consilium
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
ln -s /etc/nginx/sites-available/consilium \
  /etc/nginx/sites-enabled/consilium
nginx -t
```

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
cd /root/consilium
docker-compose logs -f consilium
```

Перезапуск:

```bash
cd /root/consilium
docker-compose restart consilium
```

Проверка:

```bash
cd /root/consilium
docker-compose ps
curl http://127.0.0.1:8002/api/ready
```

Остановка только «Консилиума»:

```bash
cd /root/consilium
docker-compose stop consilium
```

Команды выполняются из `/root/consilium`, поэтому они не управляют Compose-
проектом `bitrix_connector`.

## 14. Резервная копия базы

Приложение умеет делать согласованную SQLite-копию без остановки:

```bash
cd /root/consilium
docker-compose exec -T consilium \
  python scripts/backup_database.py --destination /app/backups --keep 14
ls -lah /root/consilium/backups
```

Также сохраняйте отдельно:

```text
/root/consilium/.env
/root/consilium/data/consilium.db
/root/consilium/backups/
```

## 15. Обновление

Перед обновлением:

```bash
cd /root/consilium
docker-compose exec -T consilium \
  python scripts/backup_database.py --destination /app/backups --keep 14
cp -a /root/consilium /root/consilium-rollback
```

Получите изменения из GitHub и пересоберите только этот проект:

```bash
cd /root/consilium
git status --short
git pull --ff-only
docker-compose build
docker-compose run --rm consilium python scripts/production_check.py
docker-compose up -d
docker-compose ps
curl http://127.0.0.1:8002/api/ready
```

Не используйте `docker-compose down -v`: параметр `-v` может удалить
постоянные данные.

## 16. Откат

Если новый проект мешает публикации, отключите только его:

```bash
cd /root/consilium
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
