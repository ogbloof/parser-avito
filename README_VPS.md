# Запуск бота

**Без VPS:** см. [DEPLOY.md](DEPLOY.md) — GitHub Pages + Render.

**На VPS (Ubuntu 24.04):** инструкция ниже.

## 1) Скопировать проект на сервер

С мака:

```bash
scp -r "/Users/ogbloof/Documents/parser avito" root@SERVER_IP:/opt/avito-bot
```

На сервере должны быть файлы:

- `bot.py`, `avito_parser.py`, `cian_parser.py`, `database.py`, `config.py`, `logging_config.py`, `selenium_fetcher.py`
- `requirements.txt`, `.env.example`
- `deploy/install_vps.sh`, `deploy/avito-bot.service`

## 2) Установить зависимости

На сервере:

```bash
cd /opt/avito-bot
bash deploy/install_vps.sh
```

Скрипт ставит Python 3, venv, Chrome (для Selenium) и зависимости из `requirements.txt`.

## 3) Создать `.env`

```bash
cd /opt/avito-bot
cp .env.example .env
nano .env
```

Обязательно:

- `BOT_TOKEN=...` — токен бота от @BotFather

По желанию:

- `ZENROWS_API_KEY=...` — если используешь ZenRows (иначе только Selenium)
- `SCRAPINGBEE_API_KEY=...` — запасной сервис
- `AVITO_PROXY=...` — прокси для Selenium (например `socks5://127.0.0.1:1080` или `http://host:port`), если IP сервера блокируют Авито/ЦИАН
- `PARSER_INTERVAL_MINUTES=5` — как часто планировщик запускает парсер (минуты)
- `ADMIN_USER_IDS=123456789` — твой Telegram user_id для команд /admin, /grant, /users, /stats
- `WEBAPP_URL=https://your-domain.com` — URL с HTTPS для Mini App (кнопка «Открыть приложение»)

## 4) Включить автозапуск (systemd)

```bash
sudo cp /opt/avito-bot/deploy/avito-bot.service /etc/systemd/system/avito-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now avito-bot
```

Проверка логов:

```bash
sudo journalctl -u avito-bot -f
```

Перезапуск:

```bash
sudo systemctl restart avito-bot
```

## 5) Mini App: GitHub Pages + API на VPS

**Схема:** статика (HTML/CSS/JS) — на GitHub Pages, API — на твоём VPS.

### 5.1 Деплой на GitHub Pages

1. Создай репозиторий и залей проект на GitHub.
2. В репо: **Settings → Pages → Source** — выбери **GitHub Actions**.
3. Добавь секрет **API_URL**: **Settings → Secrets → Actions** → New repository secret.  
   Имя: `API_URL`, значение: URL твоего API с HTTPS (например `https://api.example.com` или `https://your-domain.com`).
4. При push в `main` workflow сам задеплоит webapp.

Страница будет по адресу: `https://ogbloof.github.io/<repo>/` (подставь имя репо вместо `<repo>`)

### 5.2 API на VPS (nginx + HTTPS)

1. Получи домен и настрой A-запись на IP сервера.
2. Установи nginx и certbot:
   ```bash
   sudo apt install nginx certbot python3-certbot-nginx
   ```
3. Скопируй конфиг и замени YOUR_DOMAIN:
   ```bash
   sudo cp /opt/avito-bot/deploy/nginx-avito-bot.conf /etc/nginx/sites-available/avito-bot
   sudo ln -s /etc/nginx/sites-available/avito-bot /etc/nginx/sites-enabled/
   ```
4. Получи сертификат:
   ```bash
   sudo certbot --nginx -d YOUR_DOMAIN
   ```
5. В `.env` укажи:
   - `WEBAPP_URL=https://ogbloof.github.io/parser-avito/` — URL с GitHub Pages (если репо `parser-avito`)
   - API доступен по `https://YOUR_DOMAIN` (nginx проксирует на порт 8080)

### 5.3 Альтернатива: всё на VPS

Можно раздавать и статику, и API с одного VPS (как раньше). Тогда `WEBAPP_URL=https://YOUR_DOMAIN/webapp/` и в `config.js` не трогай `YOUR_API_DOMAIN` — приложение возьмёт API с того же origin.

## Локальный запуск (без VPS)

Установи зависимости, создай `.env` с `BOT_TOKEN`, при необходимости включи мобильный интернет или прокси:

```bash
pip install -r requirements.txt
# BOT_TOKEN и при необходимости AVITO_PROXY в .env или export
python bot.py
```
