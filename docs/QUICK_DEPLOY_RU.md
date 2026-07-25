# Простая инструкция: как опубликовать демонстрацию

Эта инструкция рассчитана на Ubuntu 24.04, домен и сервер с публичным IP.
Если сервера или домена ещё нет, сначала получите у системного администратора:
IP, имя домена, SSH-доступ и разрешение открыть порты 80/443.

## 1. Подготовьте архив на Windows

В PowerShell из папки проекта запустите безопасную сборку. Она сначала выполнит
тесты, а затем включит в архив только серверные файлы — без `.env`, базы,
журналов и виртуального окружения:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py
scp .\dist\consilium-release-ДАТА.tar.gz USER@SERVER_IP:/tmp/consilium-release.tar.gz
```

Точное имя архива будет напечатано на экране. Замените `ДАТА`, `USER` и
`SERVER_IP`. Можно вместо `scp` передать архив через WinSCP, сохранив его на
сервере как `/tmp/consilium-release.tar.gz`.

## 2. Подключитесь к серверу

```bash
ssh USER@SERVER_IP
```

## 3. Установите системные пакеты и разверните файлы

```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx certbot python3-certbot-nginx
sudo adduser --system --group --home /opt/consilium consilium
sudo mkdir -p /opt/consilium /etc/consilium /var/lib/consilium /var/log/consilium /var/backups/consilium
sudo tar -xzf /tmp/consilium-release.tar.gz -C /opt/consilium
sudo chown -R consilium:consilium /opt/consilium /var/lib/consilium /var/log/consilium /var/backups/consilium
sudo python3 -m venv /opt/consilium/.venv
sudo /opt/consilium/.venv/bin/pip install -r /opt/consilium/requirements.txt
```

## 4. Создайте секретные настройки

```bash
sudo cp /opt/consilium/.env.production.example /etc/consilium/consilium.env
sudo nano /etc/consilium/consilium.env
```

Вставьте настоящий ключ после `OPENAI_API_KEY=`, укажите рабочий домен в
`PUBLIC_BASE_URL` и замените `BOT_INTEGRATION_SECRET` случайным секретом:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Этот же секрет потребуется в `CONSILIUM_BOT_SECRET` MAX-бота. Сохранение в
nano: `Ctrl+O`, Enter; выход: `Ctrl+X`.

```bash
sudo chown root:consilium /etc/consilium/consilium.env
sudo chmod 640 /etc/consilium/consilium.env
```

## 5. Проверьте готовность

```bash
sudo -u consilium bash -c 'set -a; source /etc/consilium/consilium.env; set +a; /opt/consilium/.venv/bin/python /opt/consilium/scripts/production_check.py'
```

Продолжайте, только если итог содержит `0 ошибок`.

## 6. Включите приложение и резервные копии

```bash
sudo cp /opt/consilium/deploy/consilium.service /etc/systemd/system/
sudo cp /opt/consilium/deploy/consilium-backup.service /etc/systemd/system/
sudo cp /opt/consilium/deploy/consilium-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now consilium
sudo systemctl enable --now consilium-backup.timer
curl http://127.0.0.1:8000/api/ready
```

Ожидается `"status": "ready"`.

## 7. Подключите домен и HTTPS

Сначала направьте DNS A-запись домена на IP сервера. Затем:

```bash
sudo cp /opt/consilium/deploy/nginx-consilium.conf /etc/nginx/sites-available/consilium
sudo nano /etc/nginx/sites-available/consilium
```

Замените `consilium.example.ru` на свой домен, затем:

```bash
sudo ln -s /etc/nginx/sites-available/consilium /etc/nginx/sites-enabled/consilium
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d ВАШ_ДОМЕН
```

Откройте `https://ВАШ_ДОМЕН`. Не демонстрируйте через обычный HTTP.

## 8. Быстрая проверка перед встречей

- откройте сайт с телефона и компьютера;
- пройдите новый вход в приватном окне;
- отправьте тестовое сообщение и вызовите консилиум;
- проверьте сценарий «человек → созвон → номер»;
- убедитесь, что используются только вымышленные данные;
- выполните ручную копию:

```bash
sudo systemctl start consilium-backup.service
sudo systemctl status consilium-backup.service --no-pager
```

Подробная эксплуатация и устранение проблем описаны в
[`DEPLOYMENT_RU.md`](DEPLOYMENT_RU.md).
