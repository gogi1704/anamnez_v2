# Подробное развёртывание и эксплуатация «Консилиума»

## Назначение документа

Рекомендуемая конфигурация предназначена для закрытой демонстрации или
контролируемого прототипа. Все команды ниже рассчитаны на Ubuntu 24.04 LTS,
Nginx, systemd и один экземпляр приложения.

## Требования

- 2 vCPU, 4 ГБ RAM, 30 ГБ SSD;
- публичный IP и доменное имя;
- SSH-пользователь с `sudo`;
- Python 3.11+;
- исходящий HTTPS-доступ к AI API;
- входящие TCP 80/443; порт 8000 снаружи должен быть закрыт;
- действующий AI API-ключ.

Для встречи рекомендуется отдельный демонстрационный сервер и отдельный
ограниченный по бюджету API-ключ.

## Состав серверного пакета

- `backend/`, `static/`, `index.html`, `run.py` — приложение;
- `.env.production.example` — шаблон без секрета;
- `deploy/consilium.service` — автозапуск и перезапуск;
- `deploy/nginx-consilium.conf` — reverse proxy;
- `deploy/consilium-backup.*` — ежедневная резервная копия;
- `scripts/production_check.py` — предпусковой контроль;
- `scripts/backup_database.py` — согласованная SQLite-копия, gzip, SHA-256 и
  хранение последних 14 копий.
- `scripts/build_release.py` — запускает тесты и собирает архив по белому списку,
  исключая локальные секреты, базы, журналы и виртуальные окружения.

## Каталоги и права

| Путь | Назначение | Владелец |
|---|---|---|
| `/opt/consilium` | код и виртуальное окружение | `consilium:consilium` |
| `/etc/consilium/consilium.env` | секреты и production-настройки | `root:consilium`, `0640` |
| `/var/lib/consilium` | рабочая SQLite-база | `consilium:consilium` |
| `/var/log/consilium` | файл ошибок приложения | `consilium:consilium` |
| `/var/backups/consilium` | резервные копии | `consilium:consilium` |

Нельзя переносить на сервер локальные `.env`, `.venv`, базу из `data/` и журналы.

## Настройки окружения

- `APP_ENV=production` — включает ожидаемый production-профиль.
- `OPENAI_API_KEY` — секрет; не выводить в журнал и не хранить в Git.
- `ORCHESTRATOR_MODEL`, `SPECIALIST_MODEL` — модели двух контуров.
- `DATABASE_PATH` — абсолютный путь к SQLite.
- `LOG_PATH` — абсолютный путь к журналу ошибок.
- `HOST=127.0.0.1` — запрещает прямой внешний доступ к Python.
- `PORT=8000` — локальный порт между Nginx и Python.
- `AUTO_OPEN_BROWSER=0` — обязательно для headless-сервера.
- `COOKIE_SECURE=1` — cookie отправляется только через HTTPS.
- `PUBLIC_BASE_URL` — внешний HTTPS-адрес, который попадёт в одноразовую ссылку.
- `BOT_INTEGRATION_SECRET` — общий серверный секрет Консилиума и MAX-бота.
- `AUTH_LINK_TTL_SECONDS` — срок одноразовой ссылки; для сообщения в MAX
  рекомендуется 604800 секунд, то есть 7 дней.
- `SESSION_TTL_DAYS` — срок сессии браузера, по умолчанию 90 дней.

Подробная схема находится в [`MAX_AUTH_RU.md`](MAX_AUTH_RU.md).

## Проверка и запуск

После копирования файлов и создания окружения выполните предпусковую проверку
из короткой инструкции. Затем установите systemd units.

Полезные команды:

```bash
sudo systemctl status consilium --no-pager
sudo journalctl -u consilium -n 100 --no-pager
sudo journalctl -u consilium -f
curl -i http://127.0.0.1:8000/api/health
curl -i http://127.0.0.1:8000/api/ready
```

Разница:

- `/api/health` возвращает 200, если веб-процесс отвечает;
- `/api/ready` возвращает 200 только при доступной базе, настроенном AI-ключе
  и готовой MAX-авторизации.

## Nginx и HTTPS

Nginx принимает публичные запросы и передаёт их локальному Python-процессу.
Лимит 18 МБ согласован с максимальным запросом из трёх вложений. Тайм-аут 180
секунд учитывает долгие AI-ответы. Заголовок `X-Forwarded-Proto` нужен для
корректного режима Secure cookie. Для дорогих AI-маршрутов шаблон ограничивает
частоту запросов с одного IP и возвращает HTTP 429 при явном превышении.

После выпуска сертификата проверьте:

```bash
sudo nginx -t
curl -I https://ВАШ_ДОМЕН/
curl https://ВАШ_ДОМЕН/api/ready
sudo certbot renew --dry-run
```

Не добавляйте порт 8000 в публичный firewall/security group.

## Резервное копирование

Таймер запускается ежедневно около 03:15. `Persistent=true` выполнит пропущенную
задачу после включения сервера.

```bash
systemctl list-timers consilium-backup.timer
sudo systemctl start consilium-backup.service
sudo journalctl -u consilium-backup.service -n 50 --no-pager
sudo ls -lah /var/backups/consilium
```

Файл `.sha256` позволяет проверить целостность:

```bash
cd /var/backups/consilium
sha256sum -c ИМЯ_КОПИИ.db.gz.sha256
```

Копии на том же сервере не защищают от потери диска. Для реального пилота
настройте шифрованную выгрузку в отдельное хранилище и протестируйте
восстановление.

## Восстановление базы

Операция заменяет рабочую базу, поэтому сначала зафиксируйте окно обслуживания:

```bash
sudo systemctl stop consilium
sudo cp /var/lib/consilium/consilium.db /var/lib/consilium/consilium.db.before-restore
sudo gzip -dc /var/backups/consilium/ИМЯ_КОПИИ.db.gz | sudo tee /var/lib/consilium/consilium.db.restored >/dev/null
sudo chown consilium:consilium /var/lib/consilium/consilium.db.restored
sudo -u consilium sqlite3 /var/lib/consilium/consilium.db.restored 'PRAGMA quick_check;'
sudo mv /var/lib/consilium/consilium.db.restored /var/lib/consilium/consilium.db
sudo systemctl start consilium
curl http://127.0.0.1:8000/api/ready
```

Ожидаемый результат `PRAGMA quick_check` — `ok`. Пакет `sqlite3` можно поставить
через `sudo apt install sqlite3`.

## Обновление

1. Создайте ручную резервную копию.
2. Сохраните текущий каталог кода как версию для отката.
3. Распакуйте новый пакет без `.env` и базы.
4. Обновите зависимости в `.venv`.
5. Запустите тесты и `production_check.py`.
6. Перезапустите сервис и проверьте `/api/ready`.

```bash
sudo systemctl start consilium-backup.service
sudo cp -a /opt/consilium /opt/consilium.rollback
sudo -u consilium /opt/consilium/.venv/bin/python -m unittest discover -s /opt/consilium/tests -v
sudo systemctl restart consilium
curl http://127.0.0.1:8000/api/ready
```

Не удаляйте рабочую версию до успешной проверки нового релиза.

## Мониторинг

Минимум для демонстрации:

- внешний HTTPS-check `/api/health` каждую минуту;
- внутренний `/api/ready`;
- свободное место на диске;
- статус systemd;
- ошибки Nginx и приложения;
- наличие свежей резервной копии;
- расход и лимиты AI API.

Нельзя записывать в журналы AI-ключ, полный номер телефона или полный текст
медицинского диалога без отдельно утверждённой политики.

## Типовые проблемы

**502 Bad Gateway**  
Проверьте `systemctl status consilium`, `journalctl`, затем локальный
`curl http://127.0.0.1:8000/api/health`.

**`/api/ready` возвращает 503**  
Ответ покажет только контур: база или наличие ключа. Запустите
`production_check.py` от пользователя `consilium`.

**403/ошибка cookie после публикации**  
Убедитесь, что открываете HTTPS, `COOKIE_SECURE=1`, а Nginx передаёт
`X-Forwarded-Proto $scheme`.

**Долгий ответ или 504**  
Проверьте доступ сервера к AI API, лимиты ключа и `proxy_read_timeout`.

**Закончился диск**  
Проверьте базу, журналы и резервные копии. Не удаляйте базу. Сначала создайте
копию и перенесите её на другой носитель.

## Критерии допуска к демонстрации

- все автоматические тесты зелёные;
- production check: 0 ошибок;
- домен открывается только по HTTPS;
- API-ключ отдельный и ограниченный;
- `/api/ready` возвращает 200;
- резервная копия создаётся и проверяется;
- пройдены анкета, чат, консилиум и сценарий созвона;
- в демонстрации нет реальных персональных или медицинских данных;
- зрителям озвучено, что человек, оплата и анализы пока заглушки.
