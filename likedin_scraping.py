"""
LinkedIn Job Search Scraper (v2)
=================================
Improvements over the basic version:
- Actually extracts job data (title, company, location, link, posted date)
  instead of just recording the search URL.
- Retry with exponential backoff on navigation/selector failures.
- Deduplicates jobs across searches (LinkedIn recycles job IDs).
- Saves incrementally after every search so a crash doesn't lose progress.
- Structured logging instead of print statements.
- Randomized delays + realistic viewport/UA to reduce bot-detection friction.
- Config via dataclass instead of loose module-level constants.

IMPORTANT: LinkedIn's Terms of Service restrict automated scraping. This
script navigates like a normal browser and only reads publicly visible
search results, but LinkedIn can still rate-limit, CAPTCHA, or ban the
account/IP used to run it. Use responsibly, keep concurrency at 1, add
delays (already included), and prefer the official LinkedIn API/partner
integrations for anything beyond personal, low-volume use.

Run in Jupyter with: `await run()`
Run as a script with: `python linkedin_job_scraper.py`
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("linkedin_scraper")


# ============================================================
# CONFIG
# ============================================================

@dataclass
class ScraperConfig:
    job_titles: list[str] = field(default_factory=lambda: [
        "Data Analyst",
        "Data Scientist",
        "AI Intern",
        "Machine Learning Intern",
        "Python Developer",
        "SQL Developer",
        "Data Analyst Intern",
        "Artificial Intelligence Intern",
    ])
    locations: list[str] = field(default_factory=lambda: [
        "Marrakech, Morocco",
        "Casablanca, Morocco",
        "Rabat, Morocco",
    ])
    output_file: str = "linkedin_jobs.csv"
    max_jobs_per_search: int = 25
    headless: bool = False
    min_delay_s: float = 2.5   # randomized delay range between actions
    max_delay_s: float = 5.5
    max_retries: int = 3
    viewport: dict = field(default_factory=lambda: {"width": 1366, "height": 768})
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


# ============================================================
# HELPERS
# ============================================================

def build_search_url(job: str, location: str) -> str:
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(job)}&location={quote(location)}"
    )


async def human_delay(cfg: ScraperConfig) -> None:
    await asyncio.sleep(random.uniform(cfg.min_delay_s, cfg.max_delay_s))


async def with_retries(coro_fn, cfg: ScraperConfig, description: str):
    """Run an async callable with exponential backoff on failure."""
    last_exc = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            return await coro_fn()
        except (PWTimeout, Exception) as exc:  # noqa: BLE001 - broad by design
            last_exc = exc
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.warning(
                "Attempt %d/%d failed for %s (%s). Retrying in %.1fs...",
                attempt, cfg.max_retries, description, exc, wait,
            )
            await asyncio.sleep(wait)
    log.error("All retries exhausted for %s: %s", description, last_exc)
    raise last_exc


# ============================================================
# SCRAPING LOGIC
# ============================================================

async def extract_jobs_from_page(page: Page, job: str, location: str, cfg: ScraperConfig) -> list[dict]:
    """Pull job cards from the current search results page."""
    await page.wait_for_selector("ul.jobs-search__results-list, .jobs-search-results-list", timeout=15000)

    # Scroll a bit to trigger lazy-loaded cards.
    for _ in range(3):
        await page.mouse.wheel(0, 800)
        await asyncio.sleep(random.uniform(0.5, 1.2))

    cards = await page.query_selector_all("ul.jobs-search__results-list > li")
    jobs = []

    for card in cards[: cfg.max_jobs_per_search]:
        try:
            title_el = await card.query_selector("h3.base-search-card__title")
            company_el = await card.query_selector("h4.base-search-card__subtitle")
            loc_el = await card.query_selector(".job-search-card__location")
            link_el = await card.query_selector("a.base-card__full-link")
            date_el = await card.query_selector("time")

            title = (await title_el.inner_text()).strip() if title_el else None
            company = (await company_el.inner_text()).strip() if company_el else None
            job_location = (await loc_el.inner_text()).strip() if loc_el else None
            link = (await link_el.get_attribute("href")) if link_el else None
            posted = (await date_el.get_attribute("datetime")) if date_el else None

            if not title or not link:
                continue

            jobs.append({
                "search_job_title": job,
                "search_location": location,
                "job_title": title,
                "company": company,
                "job_location": job_location,
                "posted_date": posted,
                "url": link.split("?")[0],  # strip tracking params
                "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping a card due to parse error: %s", exc)
            continue

    return jobs


async def search_one(page: Page, job: str, location: str, cfg: ScraperConfig) -> list[dict]:
    url = build_search_url(job, location)
    log.info("Searching: %-30s | %s", job, location)

    async def _go_and_extract():
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await human_delay(cfg)
        return await extract_jobs_from_page(page, job, location, cfg)

    return await with_retries(_go_and_extract, cfg, f"{job} @ {location}")


# ============================================================
# MAIN RUN
# ============================================================

async def run(cfg: ScraperConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ScraperConfig()
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    out_path = Path(cfg.output_file)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(
            viewport=cfg.viewport,
            user_agent=cfg.user_agent,
        )
        page = await context.new_page()

        for job in cfg.job_titles:
            for location in cfg.locations:
                try:
                    jobs = await search_one(page, job, location, cfg)
                except Exception as exc:  # noqa: BLE001
                    log.error("Giving up on %s @ %s: %s", job, location, exc)
                    continue

                new_jobs = [j for j in jobs if j["url"] not in seen_urls]
                for j in new_jobs:
                    seen_urls.add(j["url"])
                all_jobs.extend(new_jobs)

                log.info("  -> found %d jobs (%d new)", len(jobs), len(new_jobs))

                # Save incrementally so a crash mid-run doesn't lose data.
                pd.DataFrame(all_jobs).to_csv(out_path, index=False, encoding="utf-8-sig")

                await human_delay(cfg)

        await browser.close()

    df = pd.DataFrame(all_jobs)
    log.info("=" * 50)
    log.info("FINISHED — %d unique jobs saved to %s", len(df), out_path.resolve())
    log.info("=" * 50)
    return df


if __name__ == "__main__":
    asyncio.run(run())