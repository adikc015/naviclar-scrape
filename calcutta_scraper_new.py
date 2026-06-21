import re
import pandas as pd
import pdfplumber
import requests
import concurrent.futures
from io import BytesIO
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

class CalcuttaFacultyScraperOptimized:
    DEPARTMENTS = {
        "Botany": "https://www.caluniv.ac.in/academic/Botany.html",
        "Chemistry": "https://www.caluniv.ac.in/academic/Chemistry.html"
    }

    def __init__(self):
        self.results = []
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        
    def clean(self, text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def extract_emails(self, text):
        return list(set(re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', text, re.I)))

    def extract_phone(self, text):
        phones = re.findall(r'\b\d{10,13}\b', text)
        return phones[0] if phones else ""

    def get_rendered_pages(self):
        """Fetch all pages using a single playwright browser instance to save overhead."""
        pages_html = {}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            
            for dept, url in self.DEPARTMENTS.items():
                print(f"Fetching {dept}...")
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=60000)
                    page.wait_for_timeout(2000) 
                    pages_html[dept] = (url, page.content())
                except Exception as e:
                    print(f"Error fetching {dept}: {e}")
                finally:
                    page.close()
            browser.close()
        return pages_html

    def extract_pdf_text(self, pdf_url):
        try:
            r = self.session.get(pdf_url, timeout=30)
            if r.status_code != 200:
                return ""
            pdf_bytes = BytesIO(r.content)
            text = []
            with pdfplumber.open(pdf_bytes) as pdf:
                for page in pdf.pages[:3]: 
                    txt = page.extract_text()
                    if txt:
                        text.append(txt)
            return self.clean(" ".join(text))
        except Exception as e:
            return ""

    def extract_summary(self, pdf_text, fallback=""):
        if not pdf_text:
            return fallback
        chunks = re.split(r"\n+", pdf_text)
        candidates = [self.clean(c) for c in chunks if len(self.clean(c)) > 100]
        if candidates:
            return max(candidates, key=len)
        return fallback

    def extract_keywords(self, text):
        words = re.findall(r"[A-Za-z]{5,}", text.lower())
        stop = {"their", "which", "these", "those", "study", "research", "university", 
                "department", "professor", "associate", "assistant", "calcutta"}
        output = []
        for w in words:
            if w not in stop and w not in output:
                output.append(w)
        return output[:15]

    def parse_botany_chemistry_table(self, soup):
        for table in soup.find_all("table"):
            headers = [self.clean(th.get_text()).lower() for th in table.find_all(["th", "td"])]
            # Check if this table has a "designation" column
            if any("designation" in h for h in headers) and any("name" in h for h in headers):
                return table
        return None

    def parse_department_html(self, dept, url, html):
        soup = BeautifulSoup(html, "lxml")
        table = self.parse_botany_chemistry_table(soup)
        if not table:
            print(f"Could not find faculty table for {dept}")
            return []

        rows = table.find_all("tr")[1:] # Skip header
        faculty_data = []

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            
            try:
                first_col = cols[0]
                profile_url = ""
                profile_tag = first_col.find("a", href=True)
                if profile_tag:
                    profile_url = urljoin(url, profile_tag["href"])

                faculty_name = self.clean(first_col.get_text(" ", strip=True))
                faculty_name = re.sub(r"\[Profile\]", "", faculty_name, flags=re.I).strip()

                designation = self.clean(cols[1].get_text(" ", strip=True))
                
                if len(cols) >= 4:
                    research_area = self.clean(cols[2].get_text(" ", strip=True))
                    contact_text = self.clean(cols[3].get_text(" ", strip=True))
                else:
                    research_area = ""
                    contact_text = self.clean(cols[2].get_text(" ", strip=True))

                emails = self.extract_emails(contact_text)
                phone = self.extract_phone(contact_text)
                
                # Default summary
                keywords = [x.strip() for x in re.split(r"[,&;/]", research_area) if x.strip()]

                record = {
                    "faculty_name": faculty_name,
                    "title": designation,
                    "email": "; ".join(emails),
                    "phone": phone,
                    "research_areas": research_area,
                    "profile_url": profile_url,
                    "department_name": dept,
                    "affiliations": "University of Calcutta"
                }
                faculty_data.append(record)
            except Exception as e:
                print(f"Error parsing row in {dept}: {e}")
                
        return faculty_data

    def process_pdf_for_record(self, record):
        profile_url = record["profile_url"]
        pdf_text = ""
        if profile_url and profile_url.lower().endswith(".pdf"):
            pdf_text = self.extract_pdf_text(profile_url)
            
        summary = self.extract_summary(pdf_text, record["research_areas"])
        keywords = self.extract_keywords(summary) if summary else []
        
        record["research_summary"] = summary
        record["research_keywords"] = ", ".join(keywords)
        return record

    def run(self):
        print("Starting optimized scrape...")
        
        # Step 1: Render pages and extract basic table info
        pages_html = self.get_rendered_pages()
        all_records = []
        for dept, (url, html) in pages_html.items():
            records = self.parse_department_html(dept, url, html)
            print(f"Found {len(records)} records for {dept}.")
            all_records.extend(records)

        # Step 2: Concurrently process PDFs
        print("Processing PDFs concurrently...")
        enriched_records = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self.process_pdf_for_record, rec) for rec in all_records]
            for future in concurrent.futures.as_completed(futures):
                try:
                    enriched_records.append(future.result())
                except Exception as e:
                    print(f"PDF processing error: {e}")

        # Step 3: Save to CSV
        df = pd.DataFrame(enriched_records)
        
        # Organize columns meaningfully
        cols = [
            "faculty_name", "title", "department_name", "affiliations", 
            "email", "phone", "research_areas", "research_summary", 
            "research_keywords", "profile_url"
        ]
        # Only keep columns that exist
        cols = [c for c in cols if c in df.columns]
        df = df[cols]
        
        df.to_csv("calcutta_faculty_optimized.csv", index=False)
        print(f"\nSaved {len(df)} faculty records to 'calcutta_faculty_optimized.csv'.")
        return df

if __name__ == "__main__":
    scraper = CalcuttaFacultyScraperOptimized()
    df = scraper.run()
    print(df.head())
