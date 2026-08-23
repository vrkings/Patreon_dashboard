#!/usr/bin/env python3
"""
sync_youtube_ga4.py — VR KINGS CEO Dashboard: авто-імпорт YouTube + GA4

Тягне місячні метрики з YouTube Analytics API (v2) та Google Analytics 4
Data API і записує їх у ТОЙ САМИЙ Google Sheet, яким уже користується
CEO-дашборд (vrkings/Patreon_dashboard), через існуючий Apps Script Web App
(action=saveMonth) — той самий endpoint, який викликає index.html.

НІЧОГО не змінює в Code.gs і НЕ ЧІПАЄ Patreon-поля (paid, churned, net,
тіри тощо) — вони й далі вносяться вручну на вкладці "Внести дані".

Схема: Google Sheet "months" зберігає одну JSON-структуру на місяць
(ключ 'YYYY-MM'). Цей скрипт лише ДОДАЄ/ОНОВЛЮЄ такі поля в тій самій
структурі (namespaced, щоб нічого не зламати):

  YouTube (YouTube Analytics API):
    yt_views_video, yt_views_shorts   — перегляди, окремо video/shorts
    yt_subs_new_video, yt_subs_new_shorts — нові підписники, окремо
    yt_subs_new                       — нових підписників за період (сумарно)
    yt_subs_total                     — підписників всього (снепшот, див. КАВЕАТ нижче)
    yt_watch_hours                    — watch time, годин
    yt_views                          — (існуюче поле) авто-перезаписується
                                         як yt_views_video + yt_views_shorts,
                                         щоб старі графіки/картки не зламались

  YouTube — НЕ автоматизовано (свідомо, лишено ручним):
    yt_impressions, yt_ctr            — videoThumbnailImpressions /
                                         videoThumbnailImpressionsClickRate —
                                         це не reports.query() метрики, а
                                         частина "Reach reports" з bulk
                                         YouTube Reporting API (job → чекати
                                         генерацію → CSV) — інша механіка, ніж
                                         решта скрипта. Поля лишились у Sheet/
                                         index.html як ручний fallback-інпут,
                                         яким і були до цієї автоматизації.
                                         Можна додати окремою задачею пізніше.

  GA4 (GA4 Data API, property patreon.vrkings.tv):
    ga_sessions_yt_referral_desktop   — сесії youtube.com / referral
    ga_sessions_yt_referral_mobile    — сесії m.youtube.com / referral
    ga_sessions_yt_pinned_comment     — сесії youtube / pinned_comment (UTM)
    ga_sessions_yt_total              — сума трьох вище
    ga_scroll_25_yt / _50_yt / _75_yt / _100_yt — воронка scroll_depth, YT-сегмент
    ga_funnel_pageview_yt             — page_view, YT-сегмент (старт воронки)
    ga_funnel_cta_click_yt            — cta_click, YT-сегмент (кінець воронки)
    ga_cta_buttons_yt                 — {button_value: count} — розбивка по cta_button
    site_visitors                     — (існуюче поле) активні користувачі GA4 за період

  Службове:
    auto_sync — {at, period:{start,end}, source} — коли й за який період
                востаннє відпрацювала автоматика (видно в UI дашборда)

КАВЕАТ по yt_subs_total: YouTube Data API віддає лише ПОТОЧНИЙ (на момент
запиту) subscriberCount — не історичний "на кінець місяця X". Для
дефолтного запуску (попередній місяць, 1-го числа) це прийнятна апроксимація
(похибка — кілька днів). Для бекфілу СТАРИХ місяців (--month багато місяців
тому) це значення буде НЕ історичним, а поточним — скрипт логує явне
попередження в такому випадку і НЕ видає це за факт.

Жодного тихого запису нулів: якщо будь-який виклик API падає, або дані
виглядають підозріло порожніми — скрипт зупиняється з ненульовим кодом
виходу і НЕ чіпає існуюче значення в Sheet.

Використання:
    python sync_youtube_ga4.py                       # попередній календарний місяць
    python sync_youtube_ga4.py --month 2026-06        # бекфіл конкретного місяця
    python sync_youtube_ga4.py --month 2026-06 --dry-run
    python sync_youtube_ga4.py --allow-zero-traffic   # дозволити нульовий GA4 YT-трафік
                                                        # (замість помилки) — для рідкісних
                                                        # місяців, коли це справді очікувано

Змінні середовища: див. .env.example
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("sync_youtube_ga4")

# GA4: YouTube-атрибутовані (sessionSource, sessionMedium) пари.
# Онови, якщо на patreon.vrkings.tv зміниться UTM-схема лінків з YouTube.
GA4_YT_SOURCE_MEDIUM = {
    "ga_sessions_yt_referral_desktop": ("youtube.com", "referral"),
    "ga_sessions_yt_referral_mobile": ("m.youtube.com", "referral"),
    "ga_sessions_yt_pinned_comment": ("youtube", "pinned_comment"),
}

SCROLL_BUCKETS = ["25", "50", "75", "100"]


class SyncError(Exception):
    """Дані не заслуговують довіри — не пишемо їх у Sheet."""


# --------------------------------------------------------------------------
# Утиліти дат / env
# --------------------------------------------------------------------------

def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SyncError(f"Відсутня обов'язкова змінна середовища: {name} (див. .env.example)")
    return v


def month_bounds(month_key: str) -> tuple[dt.date, dt.date]:
    y, m = (int(x) for x in month_key.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y, m, calendar.monthrange(y, m)[1])
    return start, end


def previous_month_key(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - dt.timedelta(days=1)
    return f"{last_of_prev.year}-{last_of_prev.month:02d}"


def months_ago(month_key: str, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    y, m = (int(x) for x in month_key.split("-"))
    return (today.year - y) * 12 + (today.month - m)


# --------------------------------------------------------------------------
# YouTube Analytics API (OAuth — власний канал)
# --------------------------------------------------------------------------

def youtube_clients():
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from googleapiclient.discovery import build as gbuild

    creds = OAuthCredentials(
        None,
        refresh_token=require_env("YOUTUBE_REFRESH_TOKEN"),
        client_id=require_env("YOUTUBE_CLIENT_ID"),
        client_secret=require_env("YOUTUBE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/yt-analytics.readonly",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
    )
    yta = gbuild("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    yt_data = gbuild("youtube", "v3", credentials=creds, cache_discovery=False)
    return yta, yt_data


def fetch_youtube_metrics(start: dt.date, end: dt.date, month_key: str) -> dict[str, Any]:
    yta, yt_data = youtube_clients()
    channel_id = require_env("YOUTUBE_CHANNEL_ID")
    channel_ref = f"channel=={channel_id}"

    # 1) Базові метрики: перегляди, watch time, підписники gained/lost
    try:
        basic = yta.reports().query(
            ids=channel_ref,
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
        ).execute()
    except Exception as e:  # noqa: BLE001 — усе, що впало в API-виклику, фатальне для sync
        raise SyncError(f"YouTube Analytics API (basic metrics) помилка: {e}") from e

    if not basic.get("rows"):
        raise SyncError(
            f"YouTube Analytics API не повернув жодного рядка за {start}..{end}. "
            "Скоріш за все неправильний YOUTUBE_CHANNEL_ID або немає доступу токена до цього каналу."
        )
    views, watched_min, subs_gained, subs_lost = basic["rows"][0]

    # 2) Impressions / CTR — СВІДОМО НЕ АВТОМАТИЗОВАНО.
    #    videoThumbnailImpressions / videoThumbnailImpressionsClickRate — це не
    #    reports.query()-метрики, а частина "Reach reports" з bulk YouTube
    #    Reporting API (job → чекати генерацію → скачати CSV) — інша, значно
    #    важча механіка, ніж решта цього скрипта. Щоб не блокувати нею решту
    #    автоматики, поля yt_impressions/yt_ctr лишені РУЧНИМИ (fallback-input
    #    в index.html як і раніше) — не заповнюються тут. Можна додати окремою
    #    задачею через YouTube Reporting API, якщо знадобиться.

    # 3) Video vs Shorts розбивка
    try:
        by_type = yta.reports().query(
            ids=channel_ref,
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,subscribersGained",
            dimensions="creatorContentType",
        ).execute()
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"YouTube Analytics API (creatorContentType) помилка: {e}") from e

    views_by_type = {"VIDEO": 0, "SHORTS": 0}
    subs_by_type = {"VIDEO": 0, "SHORTS": 0}
    for row in by_type.get("rows", []):
        ctype, v, sg = row[0], row[1], row[2]
        if ctype in views_by_type:
            views_by_type[ctype] += v
            subs_by_type[ctype] += sg

    # 4) Поточний total підписників (снепшот "зараз", не історичний — див. докстрінг)
    try:
        ch = yt_data.channels().list(part="statistics", id=channel_id).execute()
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"YouTube Data API (channels.list) помилка: {e}") from e
    items = ch.get("items", [])
    if not items:
        raise SyncError(f"YouTube Data API не знайшов канал {channel_id}.")
    subs_total = int(items[0]["statistics"]["subscriberCount"])

    age = months_ago(month_key)
    if age > 1:
        log.warning(
            "yt_subs_total — це ПОТОЧНА кількість підписників (YouTube API не дає історичних "
            "знімків), а бекфілиться місяць %s (%s міс. тому). Число НЕ відображає стан на кінець "
            "того місяця — онови вручну, якщо є точніші дані з YouTube Studio за той період.",
            month_key, age,
        )

    return {
        "yt_views_video": int(views_by_type["VIDEO"]),
        "yt_views_shorts": int(views_by_type["SHORTS"]),
        "yt_subs_new_video": int(subs_by_type["VIDEO"]),
        "yt_subs_new_shorts": int(subs_by_type["SHORTS"]),
        "yt_subs_new": int(subs_gained) - int(subs_lost),
        "yt_subs_total": subs_total,
        "yt_watch_hours": round(int(watched_min) / 60, 1),
    }


def sanity_check_youtube(m: dict[str, Any]) -> None:
    total_views = m["yt_views_video"] + m["yt_views_shorts"]
    if total_views <= 0:
        raise SyncError(
            "YouTube: 0 переглядів за весь період — майже напевно помилка "
            "(неправильний канал, зламаний OAuth-токен, чи період без даних). Нічого не записано."
        )
    if m["yt_subs_total"] <= 0:
        raise SyncError("YouTube: subscriberCount <= 0 — підозрілі дані з API. Нічого не записано.")


# --------------------------------------------------------------------------
# GA4 Data API (service account)
# --------------------------------------------------------------------------

def ga4_client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account

    raw = require_env("GA4_SERVICE_ACCOUNT_JSON")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SyncError(
            "GA4_SERVICE_ACCOUNT_JSON не парситься як JSON — переконайся, що весь ключ "
            "сервісного акаунту вставлено одним рядком без переносів."
        ) from e
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)


def _yt_source_medium_filter():
    """OR-фільтр по (sessionSource, sessionMedium) для трьох YouTube-джерел."""
    from google.analytics.data_v1beta.types import Filter, FilterExpression, FilterExpressionList

    ors = []
    for source, medium in GA4_YT_SOURCE_MEDIUM.values():
        ors.append(
            FilterExpression(
                and_group=FilterExpressionList(
                    expressions=[
                        FilterExpression(
                            filter=Filter(
                                field_name="sessionSource",
                                string_filter=Filter.StringFilter(value=source),
                            )
                        ),
                        FilterExpression(
                            filter=Filter(
                                field_name="sessionMedium",
                                string_filter=Filter.StringFilter(value=medium),
                            )
                        ),
                    ]
                )
            )
        )
    return FilterExpression(or_group=FilterExpressionList(expressions=ors))


def _event_filter(event_name: str):
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    return FilterExpression(
        filter=Filter(field_name="eventName", string_filter=Filter.StringFilter(value=event_name))
    )


def _and_filter(*parts):
    from google.analytics.data_v1beta.types import FilterExpression, FilterExpressionList

    return FilterExpression(and_group=FilterExpressionList(expressions=list(parts)))


def fetch_ga4_sessions_by_source(client, property_id: str, start: dt.date, end: dt.date) -> dict[str, int]:
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    req = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="sessionSource"), Dimension(name="sessionMedium")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
    )
    try:
        resp = client.run_report(req)
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"GA4 Data API (sessions by source) помилка: {e}") from e

    out = dict.fromkeys(GA4_YT_SOURCE_MEDIUM, 0)
    for row in resp.rows:
        src, med = row.dimension_values[0].value, row.dimension_values[1].value
        sessions = int(row.metric_values[0].value)
        for key, (s, m) in GA4_YT_SOURCE_MEDIUM.items():
            if src == s and med == m:
                out[key] += sessions
    out["ga_sessions_yt_total"] = sum(out.values())
    return out


def fetch_ga4_site_visitors(client, property_id: str, start: dt.date, end: dt.date) -> int:
    from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest

    req = RunReportRequest(
        property=f"properties/{property_id}",
        metrics=[Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
    )
    try:
        resp = client.run_report(req)
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"GA4 Data API (activeUsers) помилка: {e}") from e
    if not resp.rows:
        return 0
    return int(resp.rows[0].metric_values[0].value)


def fetch_ga4_scroll_depth_yt(client, property_id: str, start: dt.date, end: dt.date, scroll_param: str) -> dict[str, int]:
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    dim_name = f"customEvent:{scroll_param}"
    req = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=dim_name)],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimension_filter=_and_filter(_event_filter("scroll_depth"), _yt_source_medium_filter()),
    )
    try:
        resp = client.run_report(req)
    except Exception as e:  # noqa: BLE001
        raise SyncError(
            f"GA4 Data API (scroll_depth) помилка: {e}. Якщо помилка про невідомий вимір "
            f"'{dim_name}' — параметр події не зареєстровано як custom dimension у "
            "GA4 → Admin → Custom definitions (див. README_YOUTUBE_GA4_SYNC.md)."
        ) from e

    out = {f"ga_scroll_{b}_yt": 0 for b in SCROLL_BUCKETS}
    for row in resp.rows:
        val = row.dimension_values[0].value.strip()
        count = int(row.metric_values[0].value)
        key = f"ga_scroll_{val}_yt"
        if key in out:
            out[key] += count
    return out


def fetch_ga4_funnel_yt(client, property_id: str, start: dt.date, end: dt.date) -> dict[str, int]:
    from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest

    out = {}
    for field, event in (("ga_funnel_pageview_yt", "page_view"), ("ga_funnel_cta_click_yt", "cta_click")):
        req = RunReportRequest(
            property=f"properties/{property_id}",
            metrics=[Metric(name="eventCount")],
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimension_filter=_and_filter(_event_filter(event), _yt_source_medium_filter()),
        )
        try:
            resp = client.run_report(req)
        except Exception as e:  # noqa: BLE001
            raise SyncError(f"GA4 Data API (funnel:{event}) помилка: {e}") from e
        out[field] = int(resp.rows[0].metric_values[0].value) if resp.rows else 0
    return out


def fetch_ga4_cta_buttons_yt(client, property_id: str, start: dt.date, end: dt.date, button_param: str) -> dict[str, int]:
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    dim_name = f"customEvent:{button_param}"
    req = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=dim_name)],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
        dimension_filter=_and_filter(_event_filter("cta_click"), _yt_source_medium_filter()),
    )
    try:
        resp = client.run_report(req)
    except Exception as e:  # noqa: BLE001
        raise SyncError(
            f"GA4 Data API (cta_button breakdown) помилка: {e}. Якщо помилка про невідомий вимір "
            f"'{dim_name}' — параметр не зареєстровано як custom dimension у GA4 Admin."
        ) from e

    out: dict[str, int] = {}
    for row in resp.rows:
        val = row.dimension_values[0].value.strip() or "(not set)"
        out[val] = out.get(val, 0) + int(row.metric_values[0].value)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def fetch_ga4_metrics(start: dt.date, end: dt.date) -> dict[str, Any]:
    property_id = require_env("GA4_PROPERTY_ID")
    scroll_param = os.environ.get("GA4_SCROLL_DEPTH_PARAM", "depth_percent")
    button_param = os.environ.get("GA4_CTA_BUTTON_PARAM", "cta_button")

    client = ga4_client()
    out: dict[str, Any] = {}
    out.update(fetch_ga4_sessions_by_source(client, property_id, start, end))
    out["site_visitors"] = fetch_ga4_site_visitors(client, property_id, start, end)
    out.update(fetch_ga4_scroll_depth_yt(client, property_id, start, end, scroll_param))
    out.update(fetch_ga4_funnel_yt(client, property_id, start, end))
    out["ga_cta_buttons_yt"] = fetch_ga4_cta_buttons_yt(client, property_id, start, end, button_param)
    return out


def sanity_check_ga4(m: dict[str, Any], allow_zero_traffic: bool) -> None:
    if m["ga_sessions_yt_total"] == 0 and not allow_zero_traffic:
        raise SyncError(
            "GA4: 0 сесій з YouTube (referral desktop+mobile, pinned_comment) за весь місяць. "
            "Підозріло — швидше за все GA4_PROPERTY_ID неправильний, сервісний акаунт не має "
            "доступу до property, або UTM-схема на сайті змінилась. Якщо це справді очікувано — "
            "перезапусти з --allow-zero-traffic. Нічого не записано."
        )


# --------------------------------------------------------------------------
# Google Sheet (через існуючий Apps Script Web App — той самий, що й index.html)
# --------------------------------------------------------------------------

def sheet_get_month(script_url: str, month_key: str) -> dict[str, Any]:
    try:
        r = requests.post(
            f"{script_url}?action=getAll",
            data=json.dumps({"action": "getAll"}),
            headers={"Content-Type": "text/plain;charset=utf-8"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"Не вдалось прочитати Sheet через Apps Script (getAll): {e}") from e
    if "error" in data:
        raise SyncError(f"Apps Script getAll повернув помилку: {data['error']}")
    return dict(data.get("months", {}).get(month_key, {}))


def sheet_save_month(script_url: str, month_key: str, data: dict[str, Any]) -> None:
    try:
        r = requests.post(
            f"{script_url}?action=saveMonth",
            data=json.dumps({"action": "saveMonth", "key": month_key, "data": data}),
            headers={"Content-Type": "text/plain;charset=utf-8"},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"Не вдалось записати в Sheet через Apps Script (saveMonth): {e}") from e
    if not resp.get("ok"):
        raise SyncError(f"Apps Script saveMonth повернув помилку: {resp}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VR KINGS dashboard: YouTube + GA4 auto-sync")
    p.add_argument("--month", help="YYYY-MM, дефолт — попередній календарний місяць")
    p.add_argument("--dry-run", action="store_true", help="Порахувати й вивести в лог, нічого не писати в Sheet")
    p.add_argument(
        "--allow-zero-traffic",
        action="store_true",
        help="Не падати, якщо GA4 YouTube-трафік за місяць == 0 (типово це помилка налаштування)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    month_key = args.month or previous_month_key()
    start, end = month_bounds(month_key)
    log.info("=== VR KINGS: YouTube + GA4 sync — %s (%s → %s) ===", month_key, start, end)

    script_url = require_env("SCRIPT_URL")

    log.info("Тягну YouTube Analytics...")
    yt = fetch_youtube_metrics(start, end, month_key)
    sanity_check_youtube(yt)
    log.info(
        "YouTube OK: views(video/shorts)=%s/%s subs_new=%s subs_total(now)=%s watch_h=%s "
        "(impressions/CTR — ручні поля, не автоматизовано)",
        yt["yt_views_video"], yt["yt_views_shorts"], yt["yt_subs_new"], yt["yt_subs_total"],
        yt["yt_watch_hours"],
    )

    log.info("Тягну GA4...")
    ga = fetch_ga4_metrics(start, end)
    sanity_check_ga4(ga, args.allow_zero_traffic)
    log.info(
        "GA4 OK: sessions_yt_total=%s (desktop=%s mobile=%s pinned=%s) site_visitors=%s "
        "funnel pageview→cta=%s→%s scroll25/50/75/100=%s/%s/%s/%s cta_buttons=%d варіантів",
        ga["ga_sessions_yt_total"],
        ga["ga_sessions_yt_referral_desktop"], ga["ga_sessions_yt_referral_mobile"], ga["ga_sessions_yt_pinned_comment"],
        ga["site_visitors"], ga["ga_funnel_pageview_yt"], ga["ga_funnel_cta_click_yt"],
        ga["ga_scroll_25_yt"], ga["ga_scroll_50_yt"], ga["ga_scroll_75_yt"], ga["ga_scroll_100_yt"],
        len(ga["ga_cta_buttons_yt"]),
    )

    computed = {**yt, **ga}
    computed["yt_views"] = yt["yt_views_video"] + yt["yt_views_shorts"]  # сумісність зі старим полем

    if args.dry_run:
        log.info("--dry-run: у Sheet нічого не записано. Обчислені значення:\n%s",
                  json.dumps(computed, indent=2, ensure_ascii=False))
        return 0

    log.info("Читаю поточний запис Sheet для %s...", month_key)
    current = sheet_get_month(script_url, month_key)
    before = {k: current.get(k) for k in computed}
    current.update(computed)
    current["key"] = month_key
    current["auto_sync"] = {
        "at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "source": "sync_youtube_ga4.py",
    }

    log.info("Пишу об'єднаний запис у Sheet (key=%s)...", month_key)
    sheet_save_month(script_url, month_key, current)

    log.info("Готово. Змінені поля (було → стало):")
    for k in sorted(computed):
        if k == "ga_cta_buttons_yt":
            continue
        log.info("  %-30s %-12s -> %s", k, before.get(k), current.get(k))
    log.info("  ga_cta_buttons_yt: %s", json.dumps(current["ga_cta_buttons_yt"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as e:
        log.error("СИНХРОНІЗАЦІЮ ЗУПИНЕНО: %s", e)
        log.error("Старі значення в Sheet НЕ змінено.")
        sys.exit(1)
