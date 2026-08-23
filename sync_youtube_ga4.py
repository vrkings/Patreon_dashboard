# YouTube + GA4 авто-імпорт — налаштування

Що це: `sync_youtube_ga4.py` + `.github/workflows/sync-dashboard-data.yml` тягнуть
YouTube Analytics і GA4 дані за минулий місяць і пишуть їх у той самий Google Sheet,
яким користується дашборд (через існуючий Apps Script Web App — `Code.gs` **не змінено**).
Patreon-поля цей процес не чіпає.

⚠️ **Impressions/CTR (`yt_impressions`, `yt_ctr`) НЕ автоматизовано — свідомо.** Ці метрики
(`videoThumbnailImpressions`, `videoThumbnailImpressionsClickRate`) не доступні через простий
`reports.query()`, яким побудований весь інший скрипт — вони є тільки через bulk **YouTube
Reporting API** (створити job → зачекати генерацію звіту → скачати CSV), це окрема й значно
важча механіка. Щоб не блокувати нею решту автоматики, ці два поля лишились **ручними** —
такими ж, якими були до цієї задачі (вносиш у "Внести дані" як і раніше). Можна додати
підтримку через YouTube Reporting API окремою задачею пізніше, якщо знадобиться.

Нижче — усе, що треба зробити вручну ОДИН РАЗ, покроково.

---

## 1. Google Cloud проєкт

1. [console.cloud.google.com](https://console.cloud.google.com) → створити проєкт (або взяти існуючий, якщо вже є для vrkings.tv).
2. **APIs & Services → Library** → увімкнути:
   - **YouTube Analytics API**
   - **YouTube Data API v3**
   - **Google Analytics Data API**

---

## 2. YouTube Analytics — OAuth для власного каналу

Сервісний акаунт не підійде для аналітики персонального/brand-каналу — потрібен OAuth
від акаунту, який керує каналом VR KINGS.

1. **APIs & Services → OAuth consent screen**:
   - User type: External (якщо це не Google Workspace акаунт) → Create.
   - App name: будь-яке (напр. "VR Kings Dashboard Sync"), підтримка/email — свій.
   - Scopes: можна пропустити тут, додамо нижче вручну.
   - Test users: додати email акаунту, що керує YouTube-каналом.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Збережи **Client ID** і **Client Secret**.
3. Отримати **refresh token** (одноразово, з машини де є браузер):
   ```bash
   pip install google-auth-oauthlib
   python3 - <<'EOF'
   from google_auth_oauthlib.flow import InstalledAppFlow
   flow = InstalledAppFlow.from_client_config(
       {"installed": {
           "client_id": "ТВІЙ_CLIENT_ID",
           "client_secret": "ТВІЙ_CLIENT_SECRET",
           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
           "token_uri": "https://oauth2.googleapis.com/token",
       }},
       scopes=[
           "https://www.googleapis.com/auth/yt-analytics.readonly",
           "https://www.googleapis.com/auth/youtube.readonly",
       ],
   )
   creds = flow.run_local_server(port=0)
   print("REFRESH TOKEN:", creds.refresh_token)
   EOF
   ```
   Відкриється браузер → увійти акаунтом, що керує YouTube-каналом VR KINGS → дозволити доступ.
   Скрипт виведе `REFRESH TOKEN: 1//...` — це і є `YOUTUBE_REFRESH_TOKEN`.
4. `YOUTUBE_CHANNEL_ID` — знайти в YouTube Studio → Налаштування → Канал → Основні →
   "ID каналу" (формат `UCxxxxxxxxxxxxxxxxxxxxxx`).

---

## 3. GA4 — сервісний акаунт (простіше за OAuth, не протухає)

1. **APIs & Services → Credentials → Create Credentials → Service account**:
   - Ім'я: напр. `vrkings-dashboard-sync`.
   - Ролей на рівні проєкту не потрібно — доступ дається окремо в GA4 (крок нижче).
   - Створи → **Keys → Add key → JSON** → завантажиться файл `*.json`.
2. Відкрий цей JSON-файл, скопіюй увесь вміст (буде потрібен як `GA4_SERVICE_ACCOUNT_JSON`).
3. У **Google Analytics** ([analytics.google.com](https://analytics.google.com)):
   - Admin → property **patreon.vrkings.tv** → **Property Access Management**.
   - **+ → Add users** → встав `client_email` з JSON-ключа (виглядає як
     `vrkings-dashboard-sync@ТВІЙ-ПРОЄКТ.iam.gserviceaccount.com`).
   - Роль: **Viewer** (Data API — тільки читання, більше не треба).
4. `GA4_PROPERTY_ID` — Admin → Property Settings → **PROPERTY ID** (просто число, без "G-").

### 3.1 Custom dimensions для scroll_depth і cta_click — підтверджено в GA4 UI (21.08.2026)

GA4 Data API не бачить параметри подій, якщо вони не зареєстровані як **custom dimensions**.
Звірено напряму в GA4 → Reports → Engagement → Events і в Custom definitions — реальна
структура на patreon.vrkings.tv:

| Що | Назва події (`event_name`) | Custom parameter (event-scoped) |
|---|---|---|
| Скрол сторінки | `scroll_depth` | `depth_percent` (значення 25/50/75/100) |
| Клік по CTA-кнопці | `cta_click` | `cta_button` (значення типу `get_instant_access_hero`, `get_instant_access_yt_...`, `get_instant_access_bot...`, `start_for_5_pricing`, `join_for_15_pricing`, `become_vr_king_pricing`, `get_full_library_pricing`) |

Це вже прописано як дефолти в `.env.example` і в самому скрипті
(`GA4_SCROLL_DEPTH_PARAM=depth_percent`, `GA4_CTA_BUTTON_PARAM=cta_button`, подія для
розбивки кнопок — `cta_click`). Секрети окремо перевизначати не обов'язково, якщо
на сайті нічого не зміниться.

⚠️ **`cta_button` — це НЕ назва події**, а назва параметра всередині події `cta_click`.
На цій же події `cta_click` зареєстровано ще два custom dimensions — `cta_value` і
`button_text` (видимий текст кнопки) — вони тут не використовуються, для розбивки
"яка кнопка скільки кліків" потрібен саме `cta_button`.

⚠️ **`scroll_depth` — це кастомна подія сайту, не плутати зі стандартною GA4-подією
`scroll`** (та вбудована, спрацьовує один раз на 90% скролу, без порогів 25/50/75/100).

Перевір, що на GA4 стороні обидва dimension зареєстровані:
- **Admin → Custom definitions → Custom dimensions** → має бути event-scoped dimension
  для `depth_percent` (подія `scroll_depth`) і для `cta_button` (подія `cta_click`).
- Якщо немає — **Create custom dimension**, Scope: Event, Event parameter: точно
  `depth_percent` / `cta_button`.
- Custom dimension починає збирати дані **тільки з моменту реєстрації вперед** — заднім
  числом GA4 їх не порахує. Якщо їх ще нема — зареєструй зараз, перші місяці після
  реєстрації можуть бути 0, доки GA4 не почне їх писати.

Якщо колись на сайті зміняться назви подій/параметрів — онови секрети
`GA4_SCROLL_DEPTH_PARAM`/`GA4_CTA_BUTTON_PARAM`, а назву події `cta_click` (вона
захардкожена в `sync_youtube_ga4.py`, бо на відміну від параметрів це не typo-схильна
конфігурація) онови прямо в коді функції `fetch_ga4_cta_buttons_yt`.

---

## 4. Google Sheet — доступ

Нічого додатково робити не треба: скрипт пише через **той самий** Apps Script Web App
URL, що вже вписаний у дашборд (`localStorage.vrkings_url` в браузері, або подивись у
Apps Script → Deploy → Manage deployments). Скопіюй цей URL у секрет `SCRIPT_URL`.

⚠️ Побічне спостереження (не в рамках цієї задачі, але вартo знати): Web App задеплоєно
з "Anyone" без автентифікації — будь-хто, хто дізнається URL, може писати в Sheet. Це вже
так і без цієї автоматизації; варто колись розглянути токен/секрет у самому запиті.

---

## 5. Секрети в GitHub Actions

Репозиторій `vrkings/Patreon_dashboard` на GitHub → **Settings → Secrets and variables
→ Actions → New repository secret**. Додати кожен окремо:

| Secret | Значення |
|---|---|
| `SCRIPT_URL` | Apps Script Web App URL (крок 4) |
| `YOUTUBE_CHANNEL_ID` | з YouTube Studio |
| `YOUTUBE_CLIENT_ID` | з OAuth client (крок 2) |
| `YOUTUBE_CLIENT_SECRET` | з OAuth client (крок 2) |
| `YOUTUBE_REFRESH_TOKEN` | отриманий одноразовим скриптом (крок 2.3) |
| `GA4_PROPERTY_ID` | число з GA4 Admin |
| `GA4_SERVICE_ACCOUNT_JSON` | увесь вміст JSON-ключа, одним блоком |
| `GA4_SCROLL_DEPTH_PARAM` | назва параметра (перевір по коду сайту) |
| `GA4_CTA_BUTTON_PARAM` | назва параметра (перевір по коду сайту) |

---

## 6. Перший запуск і перевірка

1. GitHub → вкладка **Actions** → **Sync YouTube + GA4 dashboard data** → **Run workflow**.
   - Постав `dry_run = true` для першого разу — порахує все і покаже в логах, нічого не запише.
   - Залиш `month` порожнім (візьме попередній місяць) або встав конкретний `YYYY-MM` для бекфілу.
2. Відкрий лог запуску — там будуть підсумкові числа (`YouTube OK: ...`, `GA4 OK: ...`).
3. Звір ці числа з реальністю:
   - **YouTube Studio → Analytics → Overview**, вибрати той самий місяць →
     звірити перегляди, watch time, підписників. (Impressions/CTR тут НЕ звіряти — це поле
     й далі вноситься вручну, скрипт його не чіпає, див. примітку на початку файлу.)
   - **YouTube Studio → Analytics → Content → Shorts** вкладка — звірити Shorts-перегляди окремо.
   - **GA4 → Reports → Acquisition → Traffic acquisition**, фільтр по датах місяця →
     звірити сесії по `youtube.com/referral`, `m.youtube.com/referral`.
   - **GA4 → Explore** (або Reports → Engagement → Events) → звірити кількість подій
     `scroll_depth`, `cta_click`, `cta_button` за той самий період.
4. Якщо числа збігаються (з точністю до відсотків — атрибуція GA4 ніколи не буде 1:1
   з ручним підрахунком) — запусти ще раз **без** `dry_run` (`false`), щоб реально записати.
5. Відкрий дашборд на Vercel → вкладка "Внести дані" → обери той місяць → перевір, що нові
   поля (YouTube — розширена аналітика / GA4 — YouTube трафік) заповнились, і що
   Patreon-поля (paid, churned, net, тіри) залишились такими, якими були.

Якщо workflow впав червоним — це очікувана поведінка при проблемі з даними чи доступом
(скрипт навмисно НЕ пише нулі в Sheet). Дивись повідомлення в кінці логу — там прямо
написано що саме не так (неправильний property ID, немає доступу сервісного акаунту,
не зареєстрована custom dimension, і т.д.).

---

## 7. Щомісячний режим роботи

- 1-го числа щомісяця о 06:00 UTC workflow запускається сам, тягне попередній місяць.
- Manual-поля (site_visitors, yt_views, yt_clicks, tw_clicks, reddit_clicks, ig_clicks,
  усі Patreon-поля) і далі вносяться вручну, як і раніше — автоматика їх не чіпає
  (крім `yt_views` і `site_visitors`, які тепер авто-перезаписуються з YouTube/GA4 —
  якщо треба відкоригувати вручну, це все одно можна зробити в UI, наступний авто-синк
  просто перезапише їх знову за фактичними даними API).
- Якщо API одного місяця недоступне/впало — попередні збережені дані НЕ зникають,
  просто цей місяць лишається без авто-оновлення, внось вручну як робили раніше.
