from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from cloud_state import RunGuard


LOG = logging.getLogger("weekly_news_digest")
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
USER_AGENT = "weekly-news-digest/1.0 (+https://github.com/avshabunevich/weekly-news-digest)"


@dataclass(frozen=True)
class Article:
    article_id: str
    section: str
    title: str
    url: str
    source: str
    published: str
    description: str = ""


SECTION_SPECS = {
    "world": {
        "label": "Мир — топ-3",
        "count": 3,
        "query": '(conflict OR election OR diplomacy OR disaster OR "United Nations") sourcelang:english',
        "google_query": '(war OR conflict OR election OR diplomacy OR disaster) (source:Reuters OR source:AP News) when:7d',
    },
    "denmark": {
        "label": "Дания и Копенгаген — топ-2",
        "count": 2,
        "query": '(Denmark OR Danish OR Copenhagen) sourcelang:english',
        "google_query": '(Denmark OR Danish OR Copenhagen) (government OR economy OR politics OR city) when:7d',
    },
    "economy": {
        "label": "Мировая экономика — топ-3",
        "count": 3,
        "query": '(economy OR inflation OR GDP OR "central bank" OR tariffs OR trade) sourcelang:english',
        "google_query": 'world economy inflation GDP central bank tariffs when:7d',
    },
    "migration": {
        "label": "Миграционная политика Дании и Permanent Residence",
        "count": 3,
        "query": '(Denmark migration OR Denmark immigration OR "permanent residence" Denmark OR asylum Denmark)',
        "google_query": 'Denmark (immigration OR migration OR asylum OR permanent residence) when:7d',
    },
    "astrophysics": {
        "label": "Астрофизика — топ-2",
        "count": 2,
        "query": '(domain:nasa.gov OR domain:esa.int OR domain:ras.ac.uk) (astronomy OR astrophysics OR telescope OR exoplanet OR galaxy)',
        "google_query": '(site:nasa.gov OR site:esa.int OR site:ras.ac.uk) (astronomy OR astrophysics OR telescope OR exoplanet) when:7d',
    },
}


def period(today: date | None = None) -> tuple[date, date]:
    run_date = today or datetime.now(timezone.utc).astimezone().date()
    end = run_date - timedelta(days=1)
    return end - timedelta(days=6), end


def canonical_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9а-яё]+", title.casefold()))


def collect_articles(start: date, end: date) -> list[Article]:
    collected: list[Article] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    for section, spec in SECTION_SPECS.items():
        records = google_news_records(session, str(spec["google_query"]), start, end)
        params = {
            "query": spec["query"],
            "mode": "artlist",
            "format": "json",
            "maxrecords": 45,
            "sort": "datedesc",
            "startdatetime": start.strftime("%Y%m%d000000"),
            "enddatetime": (end + timedelta(days=1)).strftime("%Y%m%d000000"),
        }
        if not records:
            try:
                response = session.get(GDELT_ENDPOINT, params=params, timeout=10)
                response.raise_for_status()
                records = response.json().get("articles", [])
            except (requests.RequestException, ValueError) as exc:
                LOG.warning("Both news sources failed for %s; GDELT error: %s", section, exc)
                records = []

        section_count = 0
        for record in records:
            title = str(record.get("title") or "").strip()
            url = str(record.get("url") or "").strip()
            searchable = f"{title} {record.get('description') or ''}".casefold()
            if section in {"denmark", "migration"} and not any(
                word in searchable for word in ("denmark", "danish", "copenhagen")
            ):
                continue
            if section == "astrophysics" and not any(
                word in searchable
                for word in ("nasa", "esa", "astronomy", "astrophysics", "telescope", "exoplanet", "galaxy")
            ):
                continue
            if not title or not url or url in seen_urls:
                continue
            title_key = canonical_key(title)
            if not title_key or title_key in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(title_key)
            domain = str(record.get("domain") or urlparse(url).netloc).removeprefix("www.")
            published = str(record.get("seendate") or "")[:8]
            article = Article(
                article_id=f"{section}-{section_count + 1}",
                section=section,
                title=title,
                url=url,
                source=domain,
                published=published,
                description=(
                    str(record.get("description") or "").strip()[:600]
                    or (fetch_description(session, url) if "news.google.com" not in urlparse(url).netloc else "")
                ),
            )
            collected.append(article)
            section_count += 1
            if section_count >= 16:
                break
    return collected


def google_news_records(
    session: requests.Session, query: str, start: date, end: date
) -> list[dict[str, str]]:
    try:
        response = session.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=20,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as exc:
        LOG.warning("Google News RSS query failed: %s", exc)
        return []

    records: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published_text = (item.findtext("pubDate") or "").strip()
        try:
            published_dt = parsedate_to_datetime(published_text).date()
        except (TypeError, ValueError, OverflowError):
            published_dt = end
        if published_dt < start or published_dt > end or not title or not link:
            continue
        source_node = item.find("source")
        source = (source_node.text or "").strip() if source_node is not None else "Google News"
        suffix = f" - {source}"
        if title.casefold().endswith(suffix.casefold()):
            title = title[: -len(suffix)].rstrip()
        description_html = item.findtext("description") or ""
        description = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)
        records.append(
            {
                "title": title,
                "url": link,
                "domain": source,
                "seendate": published_dt.strftime("%Y%m%d"),
                "description": description[:600],
            }
        )
    return records


def fetch_description(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=12, allow_redirects=True)
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "").lower():
            return ""
        soup = BeautifulSoup(response.text[:600_000], "html.parser")
        node = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if node is None:
            node = soup.find("meta", attrs={"property": "og:description"})
        value = str(node.get("content") or "").strip() if node else ""
        return re.sub(r"\s+", " ", value)[:600]
    except requests.RequestException:
        return ""


def article_payload(articles: Iterable[Article]) -> list[dict[str, str]]:
    return [
        {
            "id": item.article_id,
            "section": item.section,
            "title": item.title,
            "source": item.source,
            "date": item.published,
            "description": item.description,
            "url": item.url,
        }
        for item in articles
    ]


def digest_prompt(articles: list[Article], start: date, end: date) -> str:
    return f"""Подготовь еженедельный новостной дайджест на русском языке за {start.isoformat()}–{end.isoformat()}.
Используй ТОЛЬКО факты из переданного JSON. Не додумывай числа, причины, последствия или статус событий. Если описание скудное, честно ограничься заголовком. Удали дубли между разделами и выбери наиболее значимые материалы.

Верни только JSON-объект вида:
{{"sections":[{{"key":"world","title":"...","items":[{{"id":"world-1","title_ru":"...","summary_ru":"2–3 предложения","why_ru":"Почему важно, 1 предложение"}}]}}]}}

Нужные разделы и максимум пунктов: world=3, denmark=2, economy=3, migration=3, astrophysics=2. Для migration, если нет подтверждённых изменений правил permanent residence, добавь после отобранных новостей объект без id с title_ru «Permanent Residence», summary_ru «Подтверждённых изменений общих требований за период не обнаружено», why_ru="". Сохраняй ссылки через id — URL не изменяй.

Материалы:
{json.dumps(article_payload(articles), ensure_ascii=False)}"""


def parse_model_response(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    result = json.loads(cleaned)
    if not isinstance(result.get("sections"), list):
        raise ValueError("Model response does not contain a sections list")
    return result


def generate_digest(articles: list[Article], start: date, end: date) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if os.environ.get("ENABLE_GITHUB_MODELS", "false").casefold() != "true":
        token = ""
    if token and articles:
        prompt = digest_prompt(articles, start, end)
        try:
            response = requests.post(
                MODELS_ENDPOINT,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.environ.get("GITHUB_MODEL", "openai/gpt-4.1-mini"),
                    "messages": [
                        {"role": "system", "content": "Ты аккуратный русскоязычный редактор новостей. Отвечай валидным JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                timeout=120,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return parse_model_response(content)
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as exc:
            LOG.warning("GitHub Models generation failed; using fallback: %s", exc)
    return fallback_digest(articles)


def translate_ru(text: str, session: requests.Session) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if not value:
        return ""
    try:
        response = session.get(
            "https://api.mymemory.translated.net/get",
            params={"q": value[:480], "langpair": "en|ru"},
            timeout=20,
        )
        response.raise_for_status()
        translated = str(response.json()["responseData"]["translatedText"] or "").strip()
        if translated and translated.casefold() != value.casefold():
            return translated
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        pass
    try:
        response = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": value[:3500]},
            timeout=20,
        )
        response.raise_for_status()
        chunks = response.json()[0]
        translated = "".join(str(chunk[0] or "") for chunk in chunks)
        return translated.strip() or value
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return value


def fallback_digest(articles: list[Article]) -> dict:
    by_section: dict[str, list[Article]] = {key: [] for key in SECTION_SPECS}
    for article in articles:
        by_section.get(article.section, []).append(article)
    sections = []
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for key, spec in SECTION_SPECS.items():
        items = []
        for article in by_section[key][:{"world": 3, "denmark": 2, "economy": 3, "migration": 3, "astrophysics": 2}[key]]:
            items.append(
                {
                    "id": article.article_id,
                    "title_ru": translate_ru(article.title, session),
                    "summary_ru": translate_ru(article.description, session)
                    if article.description
                    and canonical_key(article.title) not in canonical_key(article.description)
                    else "Подробности доступны по ссылке на источник.",
                    "why_ru": "",
                }
            )
        if key == "migration" and not items:
            items.append(
                {
                    "title_ru": "Permanent Residence",
                    "summary_ru": "Подтверждённых изменений общих требований за период не обнаружено.",
                    "why_ru": "",
                }
            )
        sections.append({"key": key, "title": spec["label"], "items": items})
    return {"sections": sections}


def render(digest: dict, articles: list[Article], start: date, end: date) -> tuple[str, str]:
    lookup = {item.article_id: item for item in articles}
    html_parts = [
        '<div style="font-family:Arial,sans-serif;line-height:1.55;color:#202124;max-width:840px">',
        f"<h1>Еженедельный дайджест новостей — {end.strftime('%d.%m.%Y')}</h1>",
        f"<p><strong>Период:</strong> {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}</p>",
    ]
    text_parts = [
        f"Еженедельный дайджест новостей — {end.strftime('%d.%m.%Y')}",
        f"Период: {start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}",
        "",
    ]
    for section in digest.get("sections", []):
        title = str(section.get("title") or SECTION_SPECS.get(section.get("key"), {}).get("label") or "Раздел")
        html_parts.append(f"<h2>{html.escape(title)}</h2><ol>")
        text_parts.extend([title, ""])
        for index, item in enumerate(section.get("items", []), start=1):
            article = lookup.get(str(item.get("id") or ""))
            title_ru = str(item.get("title_ru") or (article.title if article else "Без заголовка"))
            summary = str(item.get("summary_ru") or "")
            why = str(item.get("why_ru") or "")
            source_html = ""
            source_text = ""
            if article:
                source_html = f' <a href="{html.escape(article.url, quote=True)}">Источник: {html.escape(article.source)}</a>.'
                source_text = f" Источник: {article.url}"
            why_html = f" <strong>Почему важно:</strong> {html.escape(why)}" if why else ""
            html_parts.append(
                f"<li><p><strong>{html.escape(title_ru)}</strong>. {html.escape(summary)}{why_html}{source_html}</p></li>"
            )
            text_parts.append(f"{index}. {title_ru}. {summary}{' Почему важно: ' + why if why else ''}{source_text}")
        html_parts.append("</ol>")
        text_parts.append("")
    html_parts.append("<p style=\"color:#5f6368;font-size:12px\">Автоматическая рассылка. Факты сверяйте по ссылкам на источники.</p></div>")
    return "".join(html_parts), "\n".join(text_parts)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    recipient = required_env("EMAIL_TO")
    username = required_env("SMTP_USERNAME")
    password = required_env("SMTP_PASSWORD")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def prepare_for_action(path: Path, today: date | None = None) -> None:
    start, end = period(today)
    articles = collect_articles(start, end)
    if not articles:
        raise RuntimeError("No news candidates were collected")
    payload = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "run_date": (today or date.today()).isoformat(),
        "articles": [asdict(article) for article in articles],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("digest-prompt.txt").write_text(digest_prompt(articles, start, end), encoding="utf-8")
    LOG.info("Prepared %d candidate articles for AI inference", len(articles))


def send_prepared(path: Path, response_path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    articles = [Article(**item) for item in payload["articles"]]
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    run_date = date.fromisoformat(payload["run_date"])
    report_dir = Path(os.environ.get("REPORT_DIR", "reports"))
    report_path = report_dir / f"{run_date.isoformat()}.md"
    force_send = os.environ.get("FORCE_SEND", "false").casefold() == "true"
    if report_path.exists() and not force_send:
        LOG.info("Digest %s already exists; skipping duplicate email", report_path)
        return
    try:
        digest = parse_model_response(response_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        LOG.warning("AI response was unusable; using fallback: %s", exc)
        digest = fallback_digest(articles)
    html_body, text_body = render(digest, articles, start, end)
    subject = f"Еженедельный дайджест новостей — {run_date.strftime('%d.%m.%Y')}"
    if os.environ.get("DRY_RUN", "false").casefold() == "true":
        LOG.info("Dry run requested; email was not sent")
    else:
        send_email(subject, html_body, text_body)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text_body, encoding="utf-8")
    Path("weekly-digest.html").write_text(html_body, encoding="utf-8")
    LOG.info("Digest sent and saved to %s", report_path)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--send-prepared", metavar="RESPONSE_FILE")
    args = parser.parse_args()
    run_date = date.fromisoformat(os.environ["RUN_DATE"]) if os.environ.get("RUN_DATE") else None
    if args.prepare:
        prepare_for_action(Path("digest-candidates.json"), run_date)
        return 0
    if args.send_prepared:
        send_prepared(Path("digest-candidates.json"), Path(args.send_prepared))
        return 0
    start, end = period(run_date)
    report_dir = Path(os.environ.get("REPORT_DIR", "reports"))
    report_path = report_dir / f"{(run_date or date.today()).isoformat()}.md"
    force_send = os.environ.get("FORCE_SEND", "false").casefold() == "true"
    if report_path.exists() and not force_send:
        LOG.info("Digest %s already exists; skipping duplicate email", report_path)
        return 0
    dry_run = os.environ.get("DRY_RUN", "false").casefold() == "true"
    guard = RunGuard("weekly-news-digest", f"{end.isocalendar().year}-W{end.isocalendar().week:02d}")
    if not force_send and not dry_run and not guard.acquire():
        return 0
    try:
        articles = collect_articles(start, end)
        if not articles:
            raise RuntimeError("No news candidates were collected; refusing to send an empty digest")
        LOG.info("Collected %d candidate articles", len(articles))
        digest = generate_digest(articles, start, end)
        html_body, text_body = render(digest, articles, start, end)
        subject = f"Еженедельный дайджест новостей — {(run_date or date.today()).strftime('%d.%m.%Y')}"
        if dry_run:
            LOG.info("Dry run requested; email was not sent")
        else:
            send_email(subject, html_body, text_body)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text_body, encoding="utf-8")
        Path("weekly-digest.html").write_text(html_body, encoding="utf-8")
        if not dry_run:
            guard.mark_sent()
        LOG.info("Digest sent and saved to %s", report_path)
        return 0
    except Exception as error:
        guard.mark_failed(error)
        raise


if __name__ == "__main__":
    sys.exit(main())
