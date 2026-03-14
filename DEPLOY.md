# Инструкция: деплой без VPS (GitHub Pages + Render)

Полная настройка: webapp на GitHub Pages, бот и API на Render.

---

## Часть 1. Репозиторий на GitHub

### 1.1 Создать репо

1. Зайди на https://github.com/new
2. Имя: `parser-avito` (или другое)
3. Public, **не** ставь README/gitignore
4. Create repository

### 1.2 Залить код

```bash
cd "/Users/ogbloof/Documents/parser avito"
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/ogbloof/parser-avito.git
git push -u origin main
```

---

## Часть 2. GitHub Pages (webapp)

### 2.1 Включить Pages

1. Твой репо → **Settings** → **Pages**
2. **Build and deployment** → **Source**: выбери **GitHub Actions**

### 2.2 Секрет API_URL (пока пустой, добавим после Render)

1. **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**
3. Name: `API_URL`
4. Value: пока можно `https://placeholder.render.com` — заменишь после деплоя на Render

### 2.3 Проверить деплой

- **Actions** → дождись зелёной галочки у workflow "Deploy WebApp"
- Webapp будет по адресу: **https://ogbloof.github.io/parser-avito/**

---

## Часть 3. Render (бот + API)

### 3.1 Аккаунт

1. Зайди на https://render.com
2. Sign up через GitHub (удобнее для автодеплоя)

### 3.2 Создать Web Service

1. **Dashboard** → **New** → **Web Service**
2. **Connect a repository** → выбери `ogbloof/parser-avito`
3. Если репо не виден — **Configure account** и дай Render доступ к репо

### 3.3 Настройки сервиса

| Поле | Значение |
|------|----------|
| **Name** | `avito-parser-bot` |
| **Region** | Frankfurt или любой |
| **Branch** | `main` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Instance Type** | Free |

### 3.4 Environment Variables

Нажми **Advanced** → **Add Environment Variable**. Добавь:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | твой токен от @BotFather |
| `ADMIN_USER_IDS` | твой Telegram user_id (например `123456789`) |
| `WEBAPP_URL` | `https://ogbloof.github.io/parser-avito/` |
| `ZENROWS_API_KEY` | ключ ZenRows (без него парсинг будет через Selenium; на Render это может не работать) |

Опционально:

| Key | Value |
|-----|-------|
| `PARSER_INTERVAL_MINUTES` | `5` |
| `SCRAPINGBEE_API_KEY` | если есть |

### 3.5 Создать и дождаться деплоя

1. **Create Web Service**
2. Дождись сборки (3–5 минут)
3. В логах должно быть: `Бот запущен`, `API на порту 10000`
4. Скопируй URL сервиса: `https://avito-parser-bot-xxxx.onrender.com`

### 3.6 Обновить API_URL в GitHub

1. GitHub → **Settings** → **Secrets** → **Actions**
2. Редактируй секрет **API_URL**
3. Вставь URL Render: `https://avito-parser-bot-xxxx.onrender.com`
4. Сохрани

5. Запусти деплой Pages заново: **Actions** → **Deploy WebApp** → **Run workflow**

---

## Часть 4. Бот в Telegram

1. Открой @BotFather
2. Выбери своего бота
3. **Bot Settings** → **Menu Button** → **Configure menu button**
4. Укажи URL: `https://ogbloof.github.io/parser-avito/`  
   (или можно оставить — в боте уже есть кнопка «Открыть приложение» в меню)

5. Напиши боту `/start`
6. Выдай себе подписку: `/grant ТВОЙ_USER_ID 30`  
   (свой user_id — через @userinfobot или `/myid` в боте)

---

## Важные моменты

### Render Free Tier

- **Spin-down**: сервис засыпает через 15 минут без запросов.
- Первый запрос после паузы может идти 30–50 секунд.
- Чтобы «будить» сервис, можно настроить cron-job.org: каждые 10 минут делать GET на `https://твой-сервис.onrender.com/api/user` (без initData вернёт 401, но сервис проснётся).

### Selenium на Render

- Selenium/Playwright на Render часто не работают из-за отсутствия Chromium.
- **Рекомендация**: использовать **ZENROWS_API_KEY** — парсер будет работать через API без браузера.

### Данные

- На Free Tier диск эфемерный: при перезапуске данные теряются.
- Для постоянного хранения нужна БД (PostgreSQL на Render) и миграция с SQLite.

---

## Шпаргалка URL

| Что | URL |
|-----|-----|
| Webapp | https://ogbloof.github.io/parser-avito/ |
| API (Render) | https://avito-parser-bot-xxxx.onrender.com |
| Репо | https://github.com/ogbloof/parser-avito |
