"""
Stagiaires.ma Scraper
=====================
Adapted from the LinkedIn scraper. Strategy:

1. Load listing pages by city (and optionally contract type), e.g.
   https://www.stagiaires.ma/stage-emploi-ville/marrakech
2. Read the schema.org JobPosting JSON-LD blocks embedded in the page
   (far more stable than CSS selectors), and also collect any links that
   match /stage-emploi-maroc/<id>.
3. For links not covered by JSON-LD, open the detail page and parse its JSON-LD.
4. Filter locally by keywords (Data, AI, Python, SQL, ...), dedupe by URL,
   save the CSV after every page.

NOTE: I could not inspect the live DOM, so pagination (?page=N) is a best
guess. Run once with headless=False and check the log. Also check
https://www.stagiaires.ma/robots.txt and the site's terms before running
at scale. Keep delays on and concurrency at 1.

Jupyter: `await run()`     Script: `python stagiaires_scraper.py`
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import Page, async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("stagiaires_scraper")

BASE = "https://www.stagiaires.ma"
JOB_LINK_RE = re.compile(r"/stage-emploi-maroc/(\d+)")


@dataclass
class ScraperConfig:
    cities: list[str] = field(default_factory=lambda: ["marrakech", "casablanca", "rabat"])
    contract_types: list[str | None] = field(default_factory=lambda: [None, "stage"])  # None = all types
    keywords: list[str] = field(default_factory=lambda: [
        "data", "donnée", "donnees", "analyst", "analyste", "scientist", "machine learning",
        "ia", "ai", "intelligence artificielle", "python", "sql", "bi", "power bi", "développeur python",
    ])
    filter_by_keywords: bool = True
    max_pages_per_listing: int = 5
    output_file: str = "stagiaires_jobs.csv"
    headless: bool = False
    min_delay_s: float = 2.0
    max_delay_s: float = 4.5
    max_retries: int = 3
    viewport: dict = field(default_factory=lambda: {"width": 1366, "height": 768})


# ------------------------------------------------------------ helpers

def listing_url(city: str, contract: str | None, page_no: int) -> str:
    url = f"{BASE}/stage-emploi-ville/{city}"
    params = []
    if contract:
        params.append(f"type-de-contrat={contract}")
    if page_no > 1:
        params.append(f"page={page_no}")
    return url + ("?" + "&".join(params) if params else "")


async def delay(cfg: ScraperConfig) -> None:
    await asyncio.sleep(random.uniform(cfg.min_delay_s, cfg.max_delay_s))


async def with_retries(fn, cfg: ScraperConfig, what: str):
    last = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt + random.uniform(0, 1)
            log.warning("Attempt %d/%d failed for %s (%s). Retry in %.1fs", attempt, cfg.max_retries, what, exc, wait)
            await asyncio.sleep(wait)
    raise last


def flatten_jsonld(obj) -> list[dict]:
    """JSON-LD can be a dict, a list, or use @graph. Return all JobPosting dicts."""
    out = []
    if isinstance(obj, list):
        for o in obj:
            out += flatten_jsonld(o)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            out += flatten_jsonld(obj["@graph"])
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types or obj.get("additionalType") == "JobPosting":
            out.append(obj)
    return out


def posting_to_row(p: dict, source: str) -> dict:
    org = p.get("hiringOrganization") or {}
    addr = ((p.get("jobLocation") or {}).get("address")) or {}
    if isinstance(p.get("jobLocation"), list) and p["jobLocation"]:
        addr = (p["jobLocation"][0].get("address")) or {}
    salary = p.get("baseSalary") or {}
    return {
        "job_title": (p.get("title") or "").strip() or None,
        "company": org.get("name"),
        "city": addr.get("addressLocality"),
        "contract_type": p.get("employmentType"),
        "posted_date": p.get("datePosted"),
        "valid_through": p.get("validThrough"),
        "salary_min": salary.get("minPrice"),
        "salary_max": salary.get("maxPrice"),
        "salary_currency": salary.get("currency") or salary.get("priceCurrency"),
        "description": re.sub(r"<[^>]+>", " ", p.get("description") or "").strip()[:1500] or None,
        "url": (p.get("url") or "").split("?")[0] or None,
        "source_page": source,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def read_jsonld(page: Page) -> list[dict]:
    raw = await page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    )
    postings = []
    for blob in raw:
        try:
            postings += flatten_jsonld(json.loads(blob))
        except json.JSONDecodeError:
            continue
    return postings


async def read_job_links(page: Page) -> set[str]:
    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
    return {h.split("?")[0] for h in hrefs if JOB_LINK_RE.search(h)}


def matches_keywords(row: dict, cfg: ScraperConfig) -> bool:
    if not cfg.filter_by_keywords:
        return True
    hay = f"{row.get('job_title') or ''} {row.get('description') or ''}".lower()
    # word-boundary match so "ai" doesn't hit "mail"
    return any(re.search(rf"(?<!\w){re.escape(k.lower())}(?!\w)", hay) for k in cfg.keywords)


# ------------------------------------------------------------ scraping

async def scrape_listing(page: Page, city: str, contract: str | None, cfg: ScraperConfig, seen: set[str]) -> list[dict]:
    rows: list[dict] = []
    for page_no in range(1, cfg.max_pages_per_listing + 1):
        url = listing_url(city, contract, page_no)
        log.info("Listing: %s", url)

        async def _load():
            await page.goto(url, wait_until="networkidle", timeout=45000)
            for _ in range(3):  # trigger lazy loading
                await page.mouse.wheel(0, 900)
                await asyncio.sleep(random.uniform(0.5, 1.1))

        await with_retries(_load, cfg, url)

        postings = await read_jsonld(page)
        links = await read_job_links(page)

        page_rows = [posting_to_row(p, url) for p in postings]
        covered = {r["url"] for r in page_rows if r["url"]}

        # Links on the page that had no JSON-LD -> open the detail page.
        for link in sorted(links - covered - seen):
            try:
                await delay(cfg)
                await with_retries(lambda: page.goto(link, wait_until="domcontentloaded", timeout=30000), cfg, link)
                detail = await read_jsonld(page)
                if detail:
                    row = posting_to_row(detail[0], url)
                    row["url"] = row["url"] or link
                    page_rows.append(row)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping %s: %s", link, exc)

        new = [r for r in page_rows if r["url"] and r["url"] not in seen]
        if not new:
            log.info("  no new jobs on page %d, stopping pagination", page_no)
            break
        for r in new:
            seen.add(r["url"])
        rows += new
        log.info("  page %d -> %d new jobs", page_no, len(new))
        await delay(cfg)
    return rows


async def run(cfg: ScraperConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ScraperConfig()
    all_rows: list[dict] = []
    seen: set[str] = set()
    out = Path(cfg.output_file)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(viewport=cfg.viewport, locale="fr-MA")
        page = await context.new_page()

        for city in cfg.cities:
            for contract in cfg.contract_types:
                try:
                    rows = await scrape_listing(page, city, contract, cfg, seen)
                except Exception as exc:  # noqa: BLE001
                    log.error("Giving up on %s/%s: %s", city, contract, exc)
                    continue
                kept = [r for r in rows if matches_keywords(r, cfg)]
                all_rows += kept
                log.info("  -> %d scraped, %d match keywords", len(rows), len(kept))
                pd.DataFrame(all_rows).to_csv(out, index=False, encoding="utf-8-sig")
                await delay(cfg)

        await browser.close()

    df = pd.DataFrame(all_rows)
    log.info("FINISHED - %d jobs saved to %s", len(df), out.resolve())
    return df


if __name__ == "__main__":
    asyncio.run(run())