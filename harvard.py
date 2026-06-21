import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin

try:
    from keybert import KeyBERT
    keyword_model = KeyBERT()
except Exception:
    keyword_model = None


class FacultyScraper:
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    def __init__(self):
        self.results = []

    
    # Utilities
    

    def get_soup(self, url):
        r = requests.get(url, headers=self.HEADERS, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    def clean(self, text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def normalize_email(self, text):
        text = self.clean(text)
        text = text.replace(" [at] ", "@").replace(" (at) ", "@")
        text = text.replace(" [dot] ", ".").replace(" (dot) ", ".")
        text = text.replace(" at ", "@").replace(" dot ", ".")
        m = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text, re.I)
        return m.group(0) if m else ""

    def extract_phone(self, text):
        m = re.search(r'(\+?\d[\d\-\.\s\(\)]{7,}\d)', text)
        return m.group(0) if m else ""

    def extract_keywords(self, text, top_n=8):
        if not text:
            return []
        if keyword_model:
            try:
                kws = keyword_model.extract_keywords(text, top_n=top_n, stop_words="english")
                return [k[0] for k in kws if k and k[0]]
            except Exception:
                pass
        words = re.findall(r"[A-Za-z]{5,}", text.lower())
        seen = []
        for w in words:
            if w not in seen:
                seen.append(w)
        return seen[:top_n]

    def first_nonempty(self, items):
        for x in items:
            x = self.clean(x)
            if x:
                return x
        return ""

    def text_after_label(self, soup, labels):
        labels = [x.lower() for x in labels]
        for tag in soup.find_all(["p", "div", "li", "span"]):
            txt = self.clean(tag.get_text(" ", strip=True))
            low = txt.lower()
            if any(label in low for label in labels) and len(txt) > 20:
                return txt
        return ""

    
    # Profile parser
    

    def parse_profile(self, profile_url, department_name, affiliations=None):
        soup = self.get_soup(profile_url)
        page_text = self.clean(soup.get_text(" ", strip=True))
        affiliations = affiliations or []

        faculty_name = ""
        h1 = soup.find("h1")
        if h1:
            faculty_name = self.clean(h1.get_text(" ", strip=True))
        if not faculty_name:
            faculty_name = self.first_nonempty(
                tag.get_text(" ", strip=True)
                for tag in soup.find_all(["h1", "h2"])
            )

        email = ""
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                email = self.clean(href.split(":", 1)[1].split("?")[0])
                break
        if not email:
            email = self.normalize_email(page_text)

        research_summary = ""
        candidates = []
        for p in soup.find_all("p"):
            txt = self.clean(p.get_text(" ", strip=True))
            if 120 <= len(txt) <= 1200:
                candidates.append(txt)
        if candidates:
            research_summary = max(candidates, key=len)

        if not research_summary:
            for selector in ["article", "main", ".content", ".profile", ".bio"]:
                node = soup.select_one(selector)
                if node:
                    txt = self.clean(node.get_text(" ", strip=True))
                    if len(txt) > 120:
                        research_summary = txt
                        break

        research_areas = []
        for heading in soup.find_all(["h2", "h3", "h4"]):
            htxt = self.clean(heading.get_text(" ", strip=True)).lower()
            if any(k in htxt for k in ["research areas", "research interests", "areas of research"]):
                nxt = heading.find_next(["p", "ul", "div"])
                if nxt:
                    research_areas.append(self.clean(nxt.get_text(" ", strip=True)))

        lab_name = ""
        for txt in soup.stripped_strings:
            txt = self.clean(txt)
            if "lab" in txt.lower() and len(txt) < 120:
                lab_name = txt
                break

        phone = self.extract_phone(page_text)

        # other_relevant_info = {
        #     "lab_name": lab_name,
        #     "research_areas": research_areas,
        # }

        if not affiliations:
            aff = []
            for txt in [lab_name] + research_areas:
                if txt:
                    aff.append(txt)
            affiliations = aff

        return {
            "faculty_name": faculty_name,
            "email_address": email,
            "phone": phone,
            "research_summary": research_summary,
            "research_keywords": ", ".join(
                self.extract_keywords(research_summary)
            ),
            "department_name": department_name,
            "affiliations": "; ".join(
                [a for a in affiliations if a]
            ),
            "relevant_profile_link": profile_url,
            # "other_relevant_info": other_relevant_info,
        }

    
    # Harvard Chemistry
    

    def scrape_harvard_chemistry(self):
        directory = "https://www.chemistry.harvard.edu/our-faculty"
        soup = self.get_soup(directory)
        profile_urls = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/people/" in href:
                profile_urls.add(urljoin(directory, href))

        for url in sorted(profile_urls):
            try:
                self.results.append(
                    self.parse_profile(
                        url,
                        department_name="Department of Chemistry and Chemical Biology",
                        affiliations=["Harvard Chemistry"]
                    )
                )
            except Exception as e:
                print(url, e)

    
    # Harvard MCB
    

    def scrape_harvard_mcb(self):
        directory = "https://www.mcb.harvard.edu/faculty/faculty-profiles/"
        soup = self.get_soup(directory)
        profile_urls = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/directory/" in href and "faculty-profiles" not in href:
                profile_urls.add(urljoin(directory, href))

        for url in sorted(profile_urls):
            try:
                self.results.append(
                    self.parse_profile(
                        url,
                        department_name="Department of Molecular and Cellular Biology",
                        affiliations=["Harvard MCB"]
                    )
                )
            except Exception as e:
                print(url, e)




  
    # Run
    

    def run(self, output_file="harvard_faculty_clean.csv"):
        self.scrape_harvard_chemistry()
        self.scrape_harvard_mcb()

        df = pd.DataFrame(self.results)

        cols = [
            "faculty_name",
            "email_address",
            "phone",
            "research_summary",
            "research_keywords",
            "department_name",
            "affiliations",
            "relevant_profile_link",
            # "other_relevant_info",
        ]
        df = df.reindex(columns=cols)

        df.to_csv(output_file, index=False)
        print(f"Saved {len(df)} faculty records to {output_file}")
        return df


if __name__ == "__main__":
    scraper = FacultyScraper()
    faculty_df = scraper.run()
    # print(faculty_df.head())