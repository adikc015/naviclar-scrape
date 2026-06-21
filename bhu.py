import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page, sync_playwright


BASE_URL = "https://bhu.ac.in"
OUTPUT_COLUMNS = [
    "faculty_name",
    "designation",
    "email",
    "phone",
    "research_summary",
    "research_keywords",
    "research_areas",
    "department_name",
    "affiliations",
    "profile_url",
    "personal_website",
    "google_scholar",
    "orcid",
    "researcher_id",
    "cv_url",
    "courses",
    "research_projects",
    "recent_publications",
    "education",
    "work_history",
    "thesis_supervision",
    "academic_awards",
]


@dataclass(frozen=True)
class Department:
    key: str
    name: str
    faculty_list_url: str
    output_file: str

    @property
    def unit_id(self) -> str:
        """The first two IDs uniquely identify the department on BHU pages."""
        slug = self.faculty_list_url.split("/FacultyList/", 1)[1]
        first, second, *_ = slug.split("_")
        return f"{first}_{second}"

    @property
    def profile_path(self) -> str:
        return f"/Site/FacultyProfile/{self.unit_id}"


DEPARTMENTS = {
    "botany": Department(
        "botany",
        "Botany",
        "https://bhu.ac.in/Site/FacultyList/"
        "1119_148_413_Department-of-Botany-Faculty",
        "bhu_botany_faculty.csv",
    ),
    "chemistry": Department(
        "chemistry",
        "Chemistry",
        "https://bhu.ac.in/Site/FacultyList/"
        "1_150_420_Department-of-Chemistry-Faculty",
        "bhu_chemistry_faculty.csv",
    ),
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def extract_emails(text: str) -> list[str]:
    return unique(
        match.rstrip(".,;:")
        for match in re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            text or "",
            flags=re.I,
        )
    )


def valid_http_url(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    value = re.sub(r"^https?://https?://", "https://", value, flags=re.I)
    if value.startswith("www."):
        value = f"https://{value}"
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def render(page: Page, url: str, selector: str, timeout: int = 120_000) -> str:
    """Load a BHU Angular page and wait for its API-backed content."""
    last_error = None
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_selector(selector, timeout=30_000)
            page.wait_for_timeout(350)
            return page.content()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                page.wait_for_timeout(1_500 * (attempt + 1))
    raise RuntimeError(f"Could not render {url}: {last_error}")


def section_items(soup: BeautifulSoup, section_id: str) -> list[str]:
    section = soup.select_one(section_id)
    if not section:
        return []

    heading_names = {
        clean_text(node.get_text(" ", strip=True)).casefold()
        for node in section.select("h1, h2, h3")
    }
    candidates = section.select("li, tbody tr, .interests-bg p")
    if not candidates:
        candidates = section.select("p")

    values = []
    for node in candidates:
        text = clean_text(node.get_text(" ", strip=True))
        if text and text.casefold() not in heading_names:
            values.append(text)
    return unique(values)


def labelled_value(profile: Tag | None, label: str) -> str:
    if not profile:
        return ""
    wanted = label.casefold()
    for span in profile.select("span"):
        text = clean_text(span.get_text(" ", strip=True))
        prefix = text.split(":", 1)[0].strip().casefold()
        if wanted == prefix:
            value = span.select_one("label")
            if value:
                return clean_text(value.get_text(" ", strip=True))
            return clean_text(text.split(":", 1)[-1])
    return ""


def labelled_link(profile: Tag | None, label: str, page_url: str) -> str:
    if not profile:
        return ""
    wanted = label.casefold()
    for span in profile.select("span"):
        text = clean_text(span.get_text(" ", strip=True))
        if text.split(":", 1)[0].strip().casefold() != wanted:
            continue
        anchor = span.select_one("a[href]")
        if not anchor:
            return ""
        visible_value = clean_text(anchor.get_text(" ", strip=True))
        if label == "Personal Website":
            return valid_http_url(visible_value) or valid_http_url(
                anchor.get("href", "")
            )
        return urljoin(page_url, anchor.get("href", "").strip())
    return ""


def parse_list_cards(
    html: str,
    department: Department,
) -> list[dict[str, str]]:
    """Return only cards whose profile URL belongs to this department."""
    soup = BeautifulSoup(html, "lxml")
    records = []
    seen = set()

    for card in soup.select(".stafflist"):
        anchor = card.select_one('a[href*="FacultyProfile"]')
        if not anchor:
            continue
        profile_url = urljoin(
            department.faculty_list_url, anchor.get("href", "")
        ).split("#", 1)[0]

        # BHU appends MMV/RGSC faculty to department pages. Their profile URLs
        # contain different unit IDs, so exclude them rather than mixing units.
        if department.profile_path.casefold() not in profile_url.casefold():
            continue
        if profile_url in seen:
            continue
        seen.add(profile_url)

        heading = card.select_one("h5, h4, .staff-name")
        designation = card.select_one(
            ".designation, h5 + p, h4 + p, .staff-designation"
        )
        text = clean_text(card.get_text(" ", strip=True))
        research = ""
        match = re.search(r"Research Interests?\s*:?\s*(.+)$", text, re.I)
        if match:
            research = clean_text(match.group(1))

        records.append(
            {
                "profile_url": profile_url,
                "faculty_name": clean_text(
                    heading.get_text(" ", strip=True) if heading else ""
                ),
                "designation": clean_text(
                    designation.get_text(" ", strip=True)
                    if designation
                    else ""
                ),
                "email": "; ".join(extract_emails(text)),
                "research_areas": research,
            }
        )
    return records


def parse_profile(
    html: str,
    department: Department,
    profile_url: str,
    fallback: dict[str, str],
) -> dict:
    soup = BeautifulSoup(html, "lxml")
    profile = soup.select_one(".faculty-profile")
    if not profile:
        raise ValueError("Faculty profile block was not rendered")

    name_node = profile.select_one('label[ng-bind="faculty.Name"], h4 label')
    designation_node = profile.select_one(
        'label[ng-bind="faculty.Designation"], h4 + span label'
    )
    name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
    designation = clean_text(
        designation_node.get_text(" ", strip=True)
        if designation_node
        else ""
    )
    if not name:
        name = fallback.get("faculty_name", "")
    if not designation:
        designation = fallback.get("designation", "")

    email_text = labelled_value(profile, "Email")
    emails = extract_emails(email_text) or extract_emails(
        fallback.get("email", "")
    )
    interests = section_items(soup, "#section-2")
    if not interests and fallback.get("research_areas"):
        interests = [fallback["research_areas"]]

    address = labelled_value(profile, "Address")
    affiliation = address or (
        f"Department of {department.name}, Banaras Hindu University"
    )

    return {
        "faculty_name": name,
        "designation": designation,
        "email": "; ".join(emails),
        "phone": labelled_value(profile, "Phone"),
        "research_summary": "; ".join(interests),
        "research_keywords": "; ".join(interests),
        "research_areas": "; ".join(interests),
        "department_name": department.name,
        "affiliations": affiliation,
        "profile_url": profile_url,
        "personal_website": labelled_link(
            profile, "Personal Website", profile_url
        ),
        "google_scholar": labelled_link(
            profile, "Google Scholar Profile", profile_url
        ),
        "orcid": labelled_value(profile, "ORCID Id"),
        "researcher_id": labelled_value(profile, "Researcher Id"),
        "cv_url": labelled_link(profile, "CV", profile_url),
        "courses": section_items(soup, "#section-1"),
        "research_projects": section_items(soup, "#section-3"),
        "recent_publications": section_items(soup, "#section-4"),
        "education": section_items(soup, "#section-5"),
        "work_history": section_items(soup, "#section-6"),
        "thesis_supervision": section_items(soup, "#section-7"),
        "academic_awards": section_items(soup, "#section-10"),
    }


def scrape_department(
    page: Page,
    department: Department,
    limit: int | None = None,
) -> list[dict]:
    list_html = render(page, department.faculty_list_url, ".stafflist")
    cards = parse_list_cards(list_html, department)
    if limit is not None:
        cards = cards[:limit]
    print(
        f"{department.name}: found {len(cards)} matching department profiles"
    )

    records = []
    for index, card in enumerate(cards, start=1):
        profile_url = card["profile_url"]
        try:
            html = render(page, profile_url, ".faculty-profile h4 label")
            record = parse_profile(
                html, department, profile_url, fallback=card
            )
            if record["faculty_name"]:
                records.append(record)
                print(
                    f"[{index}/{len(cards)}] OK: "
                    f"{record['faculty_name']}"
                )
            else:
                print(f"[{index}/{len(cards)}] SKIP (no name): {profile_url}")
        except Exception as exc:
            print(f"[{index}/{len(cards)}] FAILED: {profile_url} - {exc}")

    records.sort(key=lambda row: row["faculty_name"].casefold())
    return records


def csv_ready(records: list[dict]) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {}
        for column in OUTPUT_COLUMNS:
            value = record.get(column, "")
            row[column] = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (list, dict))
                else value
            )
        rows.append(row)
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape BHU faculty profiles from the correct department"
    )
    parser.add_argument(
        "--department",
        choices=("all", *DEPARTMENTS),
        default="all",
        help="Department to scrape (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Scrape only the first N matching profiles (useful for testing)",
    )
    args = parser.parse_args()

    selected = (
        DEPARTMENTS.values()
        if args.department == "all"
        else (DEPARTMENTS[args.department],)
    )
    all_records = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        )
        page = context.new_page()

        for department in selected:
            records = scrape_department(page, department, args.limit)
            all_records.extend(records)
            output = Path(department.output_file)

        context.close()
        browser.close()


    csv_ready(all_records).to_csv(
        "bhu_faculty.csv", index=False, encoding="utf-8-sig"
    )
    print(f"Total faculty scraped: {len(all_records)}")


if __name__ == "__main__":
    main()
