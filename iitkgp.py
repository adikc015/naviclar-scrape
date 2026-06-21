import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.iitkgp.ac.in"
TIMEOUT = 45
MAX_WORKERS = 6

OUTPUT_COLUMNS = [
    "name",
    "designation",
    "department",
    "email",
    "phone",
    "research_summary",
    "research_keywords",
    "research_areas",
    "affiliation",
    "profile_url",
    "personal_website",
    "bio_link",
    # "awards_link",
]


@dataclass(frozen=True)
class Department:
    code: str
    name: str
    output_file: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/department/{self.code}"


DEPARTMENTS = {
    "chemistry": Department("CY", "Chemistry", "iitkgp_chemistry_faculty.csv"),
    "bt": Department(
        "BT",
        "Bioscience and Biotechnology",
        "iitkgp_bt_faculty.csv",
    ),
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def extract_emails(text: str) -> list[str]:
    return unique(
        email.rstrip(".,;:")
        for email in re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            text,
            flags=re.I,
        )
    )


def normalize_phone(value: str) -> str:
    """Return a consistent, Excel-safe display form for an Indian number."""
    extension_match = re.search(
        r"(?:ext(?:n|ension)?\.?|x)\s*(\d{1,5})\s*$",
        value,
        flags=re.I,
    )
    extension = extension_match.group(1) if extension_match else ""
    main_value = value[: extension_match.start()] if extension_match else value
    digits = re.sub(r"\D", "", main_value)

    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]

    if digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits.startswith("3222"):
        formatted = f"+91 {digits[:4]} {digits[4:]}"
    elif len(digits) == 10 and digits[0] in "6789":
        formatted = f"+91 {digits[:5]} {digits[5:]}"
    else:
        return ""

    if extension:
        formatted += f" ext. {extension}"
    return formatted


def extract_phone(text: str) -> str:
    phone_pattern = re.compile(
        r"(?<!\d)"
        r"(?:"
        r"(?:\+?91[\s./-]?)?"
        r"(?:\(?0?3222\)?[\s./-]?)"
        r"\d{6}"
        r"|"
        r"(?:\+?91[\s./-]?)?[6-9]\d{9}"
        r")"
        r"(?:\s*(?:ext(?:n|ension)?\.?|x)\s*\d{1,5})?"
        r"(?!\d)",
        flags=re.I,
    )

    return "; ".join(
        unique(normalize_phone(match.group(0)) for match in phone_pattern.finditer(text))
    )


def make_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_with_playwright(
    url: str,
    expected_profile_path: str | None = None,
) -> str:
    """Render dynamic department pages and wait for faculty cards to load."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        navigation_error = None
        for attempt in range(3):
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
                navigation_error = None
                break
            except Exception as exc:
                navigation_error = exc
                if attempt < 2:
                    page.wait_for_timeout(3_000 * (attempt + 1))
        if navigation_error is not None:
            browser.close()
            raise navigation_error

        for select in page.locator("select").all():
            options = [
                clean_text(option.inner_text())
                for option in select.locator("option").all()
            ]
            faculty_index = next(
                (
                    index
                    for index, option in enumerate(options)
                    if option.casefold() == "faculty"
                ),
                None,
            )
            if faculty_index is not None:
                select.select_option(index=faculty_index)
                break

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

        if expected_profile_path:
            try:
                page.wait_for_function(
                    """
                    ({ path }) => {
                        const links = new Set(
                            [...document.querySelectorAll('a[href]')]
                                .map(a => a.href)
                                .filter(href => href.toLowerCase().includes(path))
                        );
                        return links.size > 1;
                    }
                    """,
                    {"path": expected_profile_path.casefold()},
                    timeout=30_000,
                )
            except Exception:
                pass

        page.wait_for_timeout(1_000)
        html = page.content()
        browser.close()
        return html


def fetch_html(
    url: str,
    expected_profile_path: str | None = None,
) -> str:
    try:
        with make_session() as session:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            html = response.text
    except requests.RequestException:
        if expected_profile_path:
            print(
                "Static department request failed; "
                "rendering the page in a browser..."
            )
        else:
            print(f"Static profile request failed; using browser fallback: {url}")
        return fetch_with_playwright(url, expected_profile_path)

    if expected_profile_path:
        soup = BeautifulSoup(html, "lxml")
        static_links = {
            urljoin(url, anchor.get("href", "")).split("#", 1)[0]
            for anchor in soup.select("a[href]")
            if expected_profile_path.casefold()
            in urljoin(url, anchor.get("href", "")).casefold()
        }
        if len(static_links) <= 1:
            print(
                "Static page contains only the HOD profile; "
                "rendering the dynamic faculty list..."
            )
            return fetch_with_playwright(url, expected_profile_path)

    return html


def profile_links(html: str, department: Department) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    expected_path = f"/department/{department.code}/faculty/"
    links = []

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        full_url = urljoin(department.url, href).split("#", 1)[0]
        if expected_path.lower() in full_url.lower():
            links.append(full_url)

    return sorted(unique(links))


def text_tokens(soup: BeautifulSoup) -> list[str]:
    return [clean_text(token) for token in soup.stripped_strings if clean_text(token)]


def token_section(
    tokens: list[str],
    start: str,
    stops: Iterable[str],
) -> list[str]:
    start_index = next(
        (i for i, token in enumerate(tokens) if token.casefold() == start.casefold()),
        None,
    )
    if start_index is None:
        return []

    stop_names = {stop.casefold() for stop in stops}
    section = []
    for token in tokens[start_index + 1 :]:
        if token.casefold() in stop_names:
            break
        section.append(token)
    return section


def find_profile_name(soup: BeautifulSoup, department_name: str) -> str:
    headings = [
        clean_text(tag.get_text(" ", strip=True))
        for tag in soup.select("h1")
    ]
    ignored = {department_name.casefold(), "faculty"}
    candidates = [name for name in headings if name.casefold() not in ignored]
    return candidates[-1] if candidates else ""


def profile_start_index(tokens: list[str], name: str) -> int:
    """Return the name occurrence in the main card, not the faculty sidebar."""
    matches = [
        index
        for index, token in enumerate(tokens)
        if token.casefold() == name.casefold()
    ]
    return matches[-1] if matches else 0


DESIGNATION_PATTERN = re.compile(
    r"\b(?:Professor|Assistant Professor|Associate Professor|"
    r"Distinguished Professor|Emeritus Professor|Visiting Professor|"
    r"Adjunct Professor|Chair Professor|Professor of Practice)\b",
    flags=re.I,
)


def is_designation(value: str) -> bool:
    value = clean_text(value)
    return bool(value and len(value) <= 120 and DESIGNATION_PATTERN.search(value))


def find_designation(
    soup: BeautifulSoup,
    tokens: list[str],
    name: str,
) -> str:
    """Read the title from the main profile card, with a page-wide fallback."""
    headings = [
        heading
        for heading in soup.select("h1, h2, h3, h4, h5, h6")
        if clean_text(heading.get_text(" ", strip=True)).casefold()
        == name.casefold()
    ]

    if headings:
        heading = headings[-1]
        container = heading.parent
        for _ in range(5):
            if container is None:
                break
            for value in container.stripped_strings:
                value = clean_text(value)
                if value.casefold() != name.casefold() and is_designation(value):
                    return value
            if len(clean_text(container.get_text(" ", strip=True))) > 1500:
                break
            container = container.parent

    for token in tokens:
        if is_designation(token):
            return clean_text(token)
    return ""


def find_phone(soup: BeautifulSoup, contact_text: str) -> str:
    """Extract only profile contact numbers, never the site-wide footer."""
    tel_numbers = []
    for anchor in soup.select('a[href^="tel:"]'):
        visible_contact = clean_text(anchor.parent.get_text(" ", strip=True))
        if visible_contact and visible_contact in contact_text:
            number = anchor["href"].split(":", 1)[1].split("?", 1)[0]
            tel_numbers.extend(extract_phone(number).split("; "))

    text_numbers = extract_phone(contact_text).split("; ")
    return "; ".join(unique(tel_numbers + text_numbers))


def labelled_link(soup: BeautifulSoup, labels: Iterable[str]) -> str:
    labels = tuple(label.casefold() for label in labels)
    for anchor in soup.select("a[href]"):
        label = clean_text(anchor.get_text(" ", strip=True)).casefold()
        if any(expected in label for expected in labels):
            href = anchor["href"].strip()
            if href and not href.casefold().startswith(("javascript:", "#")):
                return urljoin(BASE_URL, href)
    return ""


def parse_profile(profile_url: str, department: Department) -> dict[str, str]:
    html = fetch_html(profile_url)
    soup = BeautifulSoup(html, "lxml")
    tokens = text_tokens(soup)
    page_text = " ".join(tokens)

    name = find_profile_name(soup, department.name)
    profile_start = profile_start_index(tokens, name)
    designation = find_designation(soup, tokens, name)
    research_start = next(
        (
            i
            for i in range(profile_start, len(tokens))
            if tokens[i].casefold() == "research areas"
        ),
        min(profile_start + 30, len(tokens)),
    )
    contact_text = " ".join(tokens[profile_start:research_start])

    mailto_emails = [
        anchor["href"].split(":", 1)[1].split("?", 1)[0]
        for anchor in soup.select('a[href^="mailto:"]')
    ]
    emails = unique(mailto_emails + extract_emails(contact_text))

    areas = token_section(
        tokens,
        "Research Areas",
        ("Research Statement", "Selected Publications", "Current Projects"),
    )
    areas = [
        item
        for item in areas
        if item.casefold()
        not in {
            "research statement",
            "selected publications",
            "current projects",
            "group members",
        }
    ]

    research_block = token_section(
        tokens,
        "Research Statement",
        ("All Publications", "Completed Projects", "Awards and Accolades"),
    )
    keyword_parts = []
    summary_parts = []
    for item in research_block:
        if item.casefold().startswith("keywords:"):
            keyword_parts.append(re.sub(r"^keywords:\s*", "", item, flags=re.I))
        elif item.casefold() not in {
            "selected publications",
            "current projects",
            "group members",
        }:
            summary_parts.append(item)

    # Some profiles expose keywords without a populated Research Statement tab.
    if not keyword_parts:
        keyword_match = re.search(
            r"\bKeywords?\s*:\s*(.+?)(?=\s{2,}|All Publications|$)",
            page_text,
            flags=re.I,
        )
        if keyword_match:
            keyword_parts.append(keyword_match.group(1))

    return {
        "name": name,
        "designation": designation,
        "department": department.name,
        "email": "; ".join(emails),
        "phone": find_phone(soup, contact_text),
        "research_summary": clean_text(" ".join(summary_parts)),
        "research_keywords": clean_text("; ".join(keyword_parts)),
        "research_areas": "; ".join(unique(areas)),
        "affiliation": "Indian Institute of Technology Kharagpur",
        "profile_url": profile_url,
        "personal_website": labelled_link(
            soup, ("personal webpage", "personal website", "website")
        ),
        "bio_link": labelled_link(soup, ("bio sketch", "biosketch")),
        # "awards_link": labelled_link(soup, ("awards and accolades", "awards")),
    }


def scrape_department(
    department: Department,
    max_workers: int = MAX_WORKERS,
) -> pd.DataFrame:
    expected_path = f"/department/{department.code}/faculty/"
    html = fetch_html(
        department.url,
        expected_profile_path=expected_path,
    )
    print(f"Retrieved HTML for {department.name}")
    links = profile_links(html, department)
    print(f"Found {len(links)} {department.name} faculty profiles")

    records = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        jobs = {
            executor.submit(parse_profile, url, department): url for url in links
        }
        for job in as_completed(jobs):
            url = jobs[job]
            try:
                record = job.result()
                if record["name"]:
                    records.append(record)
                    print(f"OK: {record['name']}")
                else:
                    print(f"SKIP (name not found): {url}")
            except Exception as exc:
                print(f"FAILED: {url} - {exc}")

    records.sort(key=lambda row: row["name"].casefold())
    frame = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    frame.drop_duplicates(subset="profile_url", inplace=True)

    for column in ("name", "designation", "department"):
        missing = frame[column].fillna("").str.strip().eq("")
        if missing.any():
            names = ", ".join(frame.loc[missing, "name"].fillna("<unknown>"))
            print(f"WARNING: missing {column} for {missing.sum()} profiles: {names}")

    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape IIT Kharagpur faculty data"
    )
    parser.add_argument(
        "--department",
        choices=("all", *DEPARTMENTS),
        default="all",
        help="Department to scrape (default: all)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Concurrent profile requests (default: {MAX_WORKERS})",
    )
    args = parser.parse_args()

    selected = (
        DEPARTMENTS.values()
        if args.department == "all"
        else (DEPARTMENTS[args.department],)
    )

    all_frames = []

    for department in selected:
        frame = scrape_department(
            department,
            max(1, args.workers)
        )

        all_frames.append(frame)

        print(
            f"Collected {len(frame)} records "
            f"from {department.name}"
        )

    final_df = pd.concat(
        all_frames,
        ignore_index=True
    )

    final_df.to_csv(
        "iitkgp_faculty.csv",
        index=False,
        encoding="utf-8-sig"
    )

    print(
        f"Saved {len(final_df)} records "
        f"to iitkgp_faculty.csv"
    )


if __name__ == "__main__":
    main()