import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36"
)
BASE_URL = "https://umsu.unimelb.edu.au"
LISTING_URL = f"{BASE_URL}/buddy-up/clubs/clubs-listing/"

# Each worker runs its own headless Chrome, so keep this modest (each uses ~150-300 MB RAM)
MAX_WORKERS = 4

# Element(s) that appear once the club's description has rendered.
# Adjust if it turns out to be different on the club pages.
CONTENT_SELECTOR = ".mslwidget"
WAIT_SECONDS = 8  # max wait for the description to render

KEYWORDS = [
    "commerce", "finance", "financial", "accounting", "quant", "quants",
    "quantitative", "economics", "economic", "marketing", "consulting",
]
KEYWORD_PATTERN = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)

def make_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument(f"user-agent={USER_AGENT}")
    # Return from driver.get() once the HTML is parsed, without waiting for
    # fonts/trackers; WebDriverWait below still waits for the description itself
    options.page_load_strategy = "eager"
    # Skip images to speed up page loads
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2}
    )
    # Selenium 4.6+ finds/downloads the matching chromedriver itself (Selenium Manager),
    # so webdriver_manager isn't needed
    return webdriver.Chrome(options=options)


# ---------- One browser per worker thread ----------
thread_local = threading.local()
all_drivers = []
drivers_lock = threading.Lock()


def get_driver():
    if not hasattr(thread_local, "driver"):
        thread_local.driver = make_driver()
        with drivers_lock:
            all_drivers.append(thread_local.driver)
    return thread_local.driver


# ---------- 1. Get the club list ----------
listing_driver = make_driver()
try:
    listing_driver.get(LISTING_URL)
    WebDriverWait(listing_driver, 30).until(
        EC.presence_of_element_located((By.CLASS_NAME, "msl_organisation_list"))
    )
    soup = BeautifulSoup(listing_driver.page_source, "html.parser")
finally:
    listing_driver.quit()

club_data = []
for club in soup.select("li.show-item"):
    name_link = club.select_one("a.msl-gl-link")
    if name_link:
        href = name_link.get("href", "")
        club_data.append({
            "name": name_link.get_text(strip=True),
            "url": BASE_URL + href if href.startswith("/") else href,
        })

print(f"Found {len(club_data)} clubs. Scanning each page for commerce keywords...\n")


# ---------- 2. Render each club page, scan its text, collect links ----------
def get_club_text(sub_soup):
    """Text from the club's own content only, not the site-wide header/footer/nav."""
    for tag in sub_soup.select("header, footer, nav, script, style, noscript"):
        tag.decompose()
    # A page can have several .mslwidget blocks, so combine all of them
    widgets = sub_soup.select(CONTENT_SELECTOR)
    if widgets:
        return " ".join(w.get_text(" ", strip=True) for w in widgets)
    return sub_soup.body.get_text(" ", strip=True) if sub_soup.body else ""


def scrape_club(club):
    try:
        driver = get_driver()
        driver.get(club["url"])

        # Wait until the content container exists AND has some text in it
        try:
            WebDriverWait(driver, WAIT_SECONDS).until(
                lambda d: any(
                    el.text.strip()
                    for el in d.find_elements(By.CSS_SELECTOR, CONTENT_SELECTOR)
                )
            )
        except TimeoutException:
            pass  # scan whatever loaded rather than failing the club entirely

        sub_soup = BeautifulSoup(driver.page_source, "html.parser")

        # Collect links before get_club_text strips header/footer
        external_links = {
            a.get("href")
            for a in sub_soup.select("a.msl_web, a.msl_facebook, a.msl_instagram, a.msl_twitter")
            if a.get("href")
        }
        for a in sub_soup.select('.mslwidget a[href^="http"], .msl-content a[href^="http"], .msl_organisation a[href^="http"]'):
            href = a.get("href")
            if "umsu.unimelb.edu.au" not in href:
                external_links.add(href)

        text = club["name"] + " " + get_club_text(sub_soup)
        matched = sorted({m.lower() for m in KEYWORD_PATTERN.findall(text)})

        return club, sorted(external_links), matched, None
    except Exception as error:
        return club, [], [], str(error)


commerce_clubs = []
errors = []

try:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(scrape_club, club) for club in club_data]
        for i, future in enumerate(as_completed(futures), 1):
            club, links, matched, error = future.result()
            if error:
                errors.append((club, error))
            elif matched:
                commerce_clubs.append((club, links, matched))
            if i % 25 == 0:
                print(f"  ...{i}/{len(club_data)} scanned", flush=True)
finally:
    for d in all_drivers:  # always close every browser, even on Ctrl+C or a crash
        d.quit()


# ---------- 3. Print only the commerce clubs ----------
commerce_clubs.sort(key=lambda c: c[0]["name"].lower())
print()
for club, links, matched in commerce_clubs:
    print(f"Club Name: {club['name']}")
    print(f"URL: {club['url']}")
    print(f"Matched keywords: {', '.join(matched)}")
    print(f"External Links: {', '.join(links) if links else 'None found'}")
    print("-" * 30)

print(f"\n{len(commerce_clubs)} commerce clubs out of {len(club_data)} total.")
if errors:
    print(f"{len(errors)} pages failed to load:")
    for club, error in errors:
        print(f"  {club['name']}: {error}")
