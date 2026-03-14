# Чеклист деплоя (сделай сам, ~5 минут)

## Шаг 1. Render — бот и API

1. Открой **https://render.com** → Sign up → **Sign in with GitHub**

2. **New** → **Web Service**

3. Выбери репозиторий **ogbloof/parser-avito** (если нет — **Configure GitHub** и разреши доступ)

4. Заполни:
   - **Name:** `avito-parser-bot`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** Free

5. **Advanced** → **Add Environment Variable** — добавь:

   | Key | Value |
   |-----|-------|
   | BOT_TOKEN | токен из @BotFather |
   | WEBAPP_URL | `https://ogbloof.github.io/parser-avito/` |
   | ZENROWS_API_KEY | ключ с zenrows.com (обязательно для парсинга) |

   ADMIN_USER_IDS можно не заполнять.

6. **Create Web Service** → подожди 3–5 минут

7. Скопируй URL сервиса (типа `https://avito-parser-bot-xxxx.onrender.com`)

---

## Шаг 2. Связать webapp с API

1. Открой https://github.com/ogbloof/parser-avito/settings/secrets/actions

2. **New repository secret**
   - Name: `API_URL`
   - Value: URL с Render из шага 1

3. Открой https://github.com/ogbloof/parser-avito/actions

4. Запусти **Deploy WebApp to GitHub Pages** → **Run workflow**

---

## Готово

- Бот: отвечает в Telegram
- Webapp: https://ogbloof.github.io/parser-avito/
- Открой бота, /start, настрой фильтры через /set_url
