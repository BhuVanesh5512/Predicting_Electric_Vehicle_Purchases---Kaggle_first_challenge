#!/usr/bin/env python3
"""
Build a real-world 2026 India electronics dataset from live Smartprix listings.

Output columns ONLY:
    id, productname, brand, category, price, rating, release_year

The scraper never fabricates price/rating/release_year values.
Rows with no numeric customer rating are rejected, per the dataset requirement.
Release year is taken only when a release/launch/availability year is explicitly
found on the source page. It is left blank when the source does not expose one.

Notes:
- Smartprix is used because its India catalogs are broad and current, and it
  publishes prices/reviews/ratings on product pages.
- The live site UI can change. The selectors below deliberately use heuristics
  instead of one brittle CSS class.
- Respect the site's terms/robots policy and rate-limit requests.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

OUTPUT_COLUMNS = ["id", "productname", "brand", "category", "price", "rating", "release_year"]

# Category pages confirmed to exist on Smartprix from current web indexing.
# Additional categories use search pages as fallbacks.
CATEGORY_SOURCES = {
    "Mobiles": ["https://www.smartprix.com/mobiles"],
    "Laptops": ["https://www.smartprix.com/laptops"],
    "TVs": ["https://www.smartprix.com/tvs"],
    "Headphones": ["https://www.smartprix.com/mobile_headphones"],
    "Speakers": ["https://www.smartprix.com/mobile_speakers"],
    "Smartwatches": ["https://www.smartprix.com/smartwatches"],
    "Tablets": ["https://www.smartprix.com/tablets"],
    "Monitors": ["https://www.smartprix.com/monitors"],
    "Cameras": ["https://www.smartprix.com/cameras"],
    "Power banks": ["https://www.smartprix.com/power_banks"],
    # Search-based fallbacks for categories whose dedicated slug can change.
    "Gaming consoles": ["https://www.smartprix.com/products/best-products?search=gaming%20console"],
    "Gaming accessories": ["https://www.smartprix.com/products/best-products?search=gaming%20accessories"],
    "Chargers & adapters": ["https://www.smartprix.com/products/best-products?search=charger%20adapter"],
    "Storage": ["https://www.smartprix.com/products/best-products?search=SSD%20HDD%20memory%20card"],
    "Keyboards": ["https://www.smartprix.com/products/best-products?search=keyboard"],
    "Mice": ["https://www.smartprix.com/products/best-products?search=mouse"],
    "Printers": ["https://www.smartprix.com/products/best-products?search=printer"],
    "Routers / networking": ["https://www.smartprix.com/products/best-products?search=router%20wifi%20networking"],
    "Projectors": ["https://www.smartprix.com/products/best-products?search=projector"],
    "Microphones": ["https://www.smartprix.com/products/best-products?search=microphone"],
    "Webcams": ["https://www.smartprix.com/products/best-products?search=webcam"],
    "Soundbars": ["https://www.smartprix.com/products/best-products?search=soundbar"],
    "Smart-home gadgets": ["https://www.smartprix.com/products/best-products?search=smart%20home"],
    "Other computer/mobile accessories": ["https://www.smartprix.com/products/best-products?search=computer%20mobile%20accessories"],
}

# Brands explicitly requested by the user. The scraper accepts additional
# brands from the source too; this list is used for coverage reporting.
REQUESTED_BRANDS = {
    "Mobiles": {"Samsung", "Apple", "Xiaomi", "OnePlus", "Google", "Motorola", "Vivo", "Oppo", "Realme", "Nothing", "iQOO", "Poco", "Asus", "Honor"},
    "Laptops": {"HP", "Dell", "Lenovo", "Asus", "Acer", "Apple", "MSI", "Samsung", "LG", "Microsoft"},
    "TVs": {"Samsung", "LG", "Sony", "Xiaomi", "TCL", "Hisense", "OnePlus", "Panasonic", "Vu"},
    "Headphones": {"Sony", "JBL", "Bose", "Sennheiser", "Apple", "Samsung", "Xiaomi", "OnePlus", "boAt", "Noise"},
    "Speakers": {"Sony", "JBL", "Bose", "Sennheiser", "Apple", "Samsung", "Xiaomi", "OnePlus", "boAt", "Noise"},
    "Cameras": {"Sony", "Canon", "Nikon", "Fujifilm", "Panasonic", "GoPro", "DJI"},
    "Storage": {"Samsung", "SanDisk", "Kingston", "WD", "Seagate", "Crucial"},
    "Routers / networking": {"TP-Link", "Netgear", "Asus", "D-Link", "Tenda"},
}

# Brand normalization avoids spelling/casing duplicates while retaining real brand names.
BRAND_ALIASES = {
    "oppo": "Oppo", "OPPO": "Oppo", "vivo": "Vivo", "VIVO": "Vivo",
    "realme": "Realme", "REALME": "Realme", "oneplus": "OnePlus", "OnePlus": "OnePlus",
    "iqoo": "iQOO", "iQOO": "iQOO", "poco": "Poco", "POCO": "Poco",
    "boat": "boAt", "boAt": "boAt", "dji": "DJI", "DJI": "DJI",
    "hp": "HP", "HP": "HP", "asus": "Asus", "ASUS": "Asus",
    "msi": "MSI", "MSI": "MSI", "wd": "WD", "WD": "WD",
    "tp-link": "TP-Link", "TP Link": "TP-Link", "tenda": "Tenda", "Tenda": "Tenda",
}

# Broad known-brand set for conservative brand extraction from product titles.
KNOWN_BRANDS = set(BRAND_ALIASES.values()) | {
    "Samsung", "Apple", "Xiaomi", "Google", "Motorola", "Oppo", "Vivo", "Realme", "Nothing",
    "Honor", "Sony", "JBL", "Bose", "Sennheiser", "Noise", "Canon", "Nikon", "Fujifilm",
    "Panasonic", "GoPro", "HP", "Dell", "Lenovo", "Acer", "MSI", "LG", "Microsoft", "TCL",
    "Hisense", "OnePlus", "Vu", "SanDisk", "Kingston", "Seagate", "Crucial", "Netgear", "Asus",
    "D-Link", "boAt", "DJI", "Fastrack", "Fire-Boltt", "Fire Boltt", "Amazfit", "Garmin", "Fossil",
    "Logitech", "Razer", "Zebronics", "Portronics", "Anker", "UGREEN", "Belkin", "TP-Link", "Tenda",
}

PRICE_RE = re.compile(r"₹\s*([0-9][0-9,]*(?:\.\d+)?)")
RATING_RE = re.compile(r"(?<!\d)([0-5](?:\.\d)?)\s*(?:/\s*5|out of 5|stars?|$)", re.I)
RATING_COUNT_RE = re.compile(r"([0-9][0-9,]*)\s+(?:ratings?|votes?)", re.I)
YEAR_RE = re.compile(r"(?<!\d)(20(?:0\d|1\d|2[0-6]))(?!\d)")

@dataclass
class Product:
    productname: str
    brand: str
    category: str
    price: Optional[int]
    rating: Optional[float]
    release_year: Optional[int]
    url: str


def normalize_price(raw: str) -> Optional[int]:
    try:
        value = raw.replace(",", "").strip()
        return int(float(value))
    except (ValueError, TypeError):
        return None


def normalize_rating(raw: str) -> Optional[float]:
    try:
        x = float(raw)
        return x if 0 <= x <= 5 else None
    except (TypeError, ValueError):
        return None


def normalize_brand(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip()
    return BRAND_ALIASES.get(raw, BRAND_ALIASES.get(raw.lower(), raw.title() if raw.isupper() else raw))


def infer_brand(name: str, body_text: str) -> Optional[str]:
    # Prefer an explicit "By BRAND" string from Smartprix product pages.
    m = re.search(r"\bBy\s+([A-Za-z0-9&'._-]+(?:\s+[A-Za-z0-9&'._-]+){0,2})\b", body_text)
    if m:
        candidate = m.group(1).strip(" .,")
        # Stop common page words.
        candidate = re.split(r"\s+is\s+|\s+price\s+|\s+This\s+", candidate, maxsplit=1, flags=re.I)[0]
        if candidate and len(candidate) <= 40:
            return normalize_brand(candidate)

    # Match known brands against title.
    low = name.lower()
    for b in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if b.lower() in low:
            return normalize_brand(b)
    # Conservative first-token fallback; only use it if it looks like a brand.
    first = name.split()[0].strip("-_:,")
    if first and first[0].isupper() and len(first) <= 25:
        return normalize_brand(first)
    return None


def find_release_year(text: str) -> Optional[int]:
    # Only accept years tied to an explicit release/launch/availability phrase.
    patterns = [
        r"(?:available from|launched(?: on)?|launch date|release date|released(?: on)?|introduced(?: on)?)\D{0,50}(20(?:0\d|1\d|2[0-6]))",
        r"(20(?:0\d|1\d|2[0-6]))\D{0,15}(?:release|launch|available from)",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return int(m.group(1))
    return None


def build_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--lang=en-IN")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0 Safari/537.36")
    return webdriver.Chrome(options=options)


def gentle_scroll_and_load_more(driver: webdriver.Chrome, rounds: int = 40, delay: float = 0.75) -> None:
    last_height = 0
    stable_rounds = 0
    for _ in range(rounds):
        # Scroll in steps so lazy/infinite content has a chance to render.
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(delay)

        clicked = False
        for element in driver.find_elements(By.XPATH, "//button | //a | //div[@role='button']"):
            try:
                label = (element.text or "").strip().lower()
                if label and any(k in label for k in ("load more", "show more", "view more")):
                    if element.is_displayed() and element.is_enabled():
                        driver.execute_script("arguments[0].click();", element)
                        clicked = True
                        time.sleep(delay)
                        break
            except Exception:
                continue

        height = int(driver.execute_script("return document.body.scrollHeight"))
        if height == last_height and not clicked:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        last_height = height


def extract_product_links(driver: webdriver.Chrome, category_url: str) -> list[str]:
    soup = BeautifulSoup(driver.page_source, "html.parser")
    candidates: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.smartprix.com" + href
        if not href.startswith("https://www.smartprix.com/"):
            continue
        # Smartprix product pages generally use /<category>/<slug>-ppd<id>
        if "-ppd" not in href:
            continue
        if any(x in href for x in ("/compare", "/cpd", "/bytes/")):
            continue
        candidates.add(href.split("?")[0])
    return sorted(candidates)


def extract_card_candidates(driver: webdriver.Chrome, category: str) -> list[Product]:
    soup = BeautifulSoup(driver.page_source, "html.parser")
    rows: dict[str, Product] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.smartprix.com" + href
        if "-ppd" not in href or not href.startswith("https://www.smartprix.com/"):
            continue
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if len(name) < 5 or len(name) > 240:
            continue
        parent = a
        best_text = name
        for _ in range(5):
            parent = parent.parent
            if parent is None:
                break
            txt = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()
            if "₹" in txt and len(txt) < 1600:
                best_text = txt
                break
        price_match = PRICE_RE.search(best_text)
        price = normalize_price(price_match.group(1)) if price_match else None
        rating = None
        # Only accept ratings where a rating-count/votes cue exists nearby.
        count_match = RATING_COUNT_RE.search(best_text)
        if count_match:
            before = best_text[: max(0, count_match.start())]
            candidates = list(RATING_RE.finditer(before[-60:]))
            if candidates:
                rating = normalize_rating(candidates[-1].group(1))
        brand = infer_brand(name, best_text)
        year = find_release_year(best_text)
        if brand and price is not None:
            rows[href] = Product(name, brand, category, price, rating, year, href)
    return list(rows.values())


def enrich_from_detail(driver: webdriver.Chrome, product: Product, wait_seconds: int = 12) -> Product:
    try:
        driver.get(product.url)
        WebDriverWait(driver, wait_seconds).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        text = re.sub(r"\s+", " ", driver.find_element(By.TAG_NAME, "body").text).strip()
        soup = BeautifulSoup(driver.page_source, "html.parser")

        if product.price is None:
            m = PRICE_RE.search(text)
            if m:
                product.price = normalize_price(m.group(1))

        if product.rating is None:
            # Prefer the "User Reviews" section / rating line.
            idx = text.lower().find("user reviews")
            chunk = text[idx: idx + 1000] if idx >= 0 else text
            rating_matches = list(RATING_RE.finditer(chunk))
            if rating_matches:
                for m in rating_matches:
                    x = normalize_rating(m.group(1))
                    if x is not None:
                        product.rating = x
                        break
            if product.rating is None:
                # Last-resort rating with explicit ratings/votes cue.
                m = re.search(r"([0-5](?:\.\d)?)\s+([0-9][0-9,]*)\s+(?:ratings?|votes?)", text, re.I)
                if m:
                    product.rating = normalize_rating(m.group(1))

        # Product page often has "By BRAND" explicitly.
        b = infer_brand(product.productname, text)
        if b:
            product.brand = b

        if product.release_year is None:
            product.release_year = find_release_year(text)

        # Check JSON-LD / meta values for an explicit release date.
        if product.release_year is None:
            for tag in soup.find_all(["meta", "script"]):
                raw = tag.get("content") if tag.name == "meta" else tag.get_text(" ", strip=True)
                if raw and re.search(r"release|launch|available", raw, re.I):
                    y = YEAR_RE.search(raw)
                    if y:
                        product.release_year = int(y.group(1))
                        break
    except (WebDriverException, TimeoutException):
        pass
    return product


def category_from_query_or_name(category: str, name: str) -> str:
    # Keep the requested taxonomy; only use title cues inside a generic category.
    if category != "Other computer/mobile accessories":
        return category
    low = name.lower()
    rules = [
        ("Chargers & adapters", ["charger", "adapter", "travel adapter"]),
        ("Power banks", ["power bank"]),
        ("Storage", ["ssd", "hard disk", "hdd", "memory card", "micro sd", "usb drive", "pen drive"]),
        ("Keyboards", ["keyboard"]),
        ("Mice", ["mouse", "wireless mouse"]),
        ("Webcams", ["webcam"]),
        ("Microphones", ["microphone", "mic "]),
        ("Projectors", ["projector"]),
        ("Soundbars", ["soundbar"]),
        ("Routers / networking", ["router", "wifi", "wi-fi", "mesh"]),
        ("Printers", ["printer", "ink tank", "laser printer"]),
        ("Gaming consoles", ["playstation", "xbox", "switch console"]),
        ("Gaming accessories", ["gaming mouse", "gaming keyboard", "controller", "gamepad", "joystick"]),
        ("Smart-home gadgets", ["smart bulb", "smart plug", "smart camera", "smart lock", "smart home", "alexa", "google nest"]),
        ("Headphones", ["headphone", "earbuds", "earphone", "neckband"]),
        ("Speakers", ["speaker", "bluetooth speaker"]),
    ]
    for label, keys in rules:
        if any(k in low for k in keys):
            return label
    return category


def add_page_param(base_url: str, page: int) -> str:
    # Smartprix category pages use ?page=N pagination.
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page}"


def scrape(target: int, headless: bool, max_pages_per_source: int = 250, pause: float = 1.0) -> list[Product]:
    driver = build_driver(headless=headless)
    products: dict[tuple[str, str, str], Product] = {}
    try:
        # Crawl real Smartprix pagination. The current site exposes ~20-30
        # products per page, so a single page cannot produce 5,000 rows.
        for category, urls in CATEGORY_SOURCES.items():
            if len(products) >= target:
                break

            for base_url in urls:
                consecutive_empty = 0
                previous_count = len(products)

                for page in range(1, max_pages_per_source + 1):
                    if len(products) >= target:
                        break

                    url = base_url if page == 1 else add_page_param(base_url, page)
                    try:
                        print(f"[+] {category} page {page}: {url}", flush=True)
                        driver.get(url)
                        WebDriverWait(driver, 20).until(
                            EC.presence_of_element_located((By.TAG_NAME, "body"))
                        )

                        # A shorter scroll budget is enough per paginated page.
                        gentle_scroll_and_load_more(driver, rounds=12, delay=max(0.35, pause))
                        batch = extract_card_candidates(driver, category)
                        print(f"    candidates: {len(batch)}", flush=True)

                        new_on_page = 0
                        for p in batch:
                            p.category = category_from_query_or_name(p.category, p.productname)
                            key = (
                                p.productname.casefold(),
                                p.brand.casefold(),
                                p.category.casefold(),
                            )
                            if key not in products:
                                products[key] = p
                                new_on_page += 1

                        print(f"    new unique: {new_on_page}; total candidates: {len(products)}", flush=True)

                        if new_on_page == 0:
                            consecutive_empty += 1
                        else:
                            consecutive_empty = 0

                        # Stop after several pages that yield nothing new.
                        if consecutive_empty >= 3:
                            break

                        # If the site has fewer products than the requested page,
                        # stop naturally once the page stops yielding new records.
                        time.sleep(pause)

                    except Exception as exc:
                        print(f"    ! page error: {exc}", file=sys.stderr, flush=True)
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            break

                if len(products) == previous_count and len(products) > 0:
                    continue

        # Enrich only where rating is missing. Release year remains blank unless
        # the source explicitly provides one; no fabricated years.
        valid: list[Product] = []
        seen = set()
        for i, p in enumerate(products.values(), 1):
            if p.rating is None:
                p = enrich_from_detail(driver, p)
            if p.rating is None or p.price is None:
                continue
            if p.release_year is not None and not (2000 <= p.release_year <= 2026):
                p.release_year = None

            key = (
                p.productname.casefold(),
                p.brand.casefold(),
                p.category.casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            valid.append(p)

            if len(valid) % 100 == 0:
                print(f"[+] valid rows: {len(valid)}", flush=True)
            if len(valid) >= target:
                break

        return valid
    finally:
        driver.quit()


def write_csv(rows: Iterable[Product], path: str) -> None:
    rows = list(rows)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_COLUMNS)
        for idx, p in enumerate(rows, 1):
            writer.writerow([
                idx,
                p.productname,
                p.brand,
                p.category,
                p.price,
                p.rating,
                p.release_year if p.release_year is not None else "",
            ])


def report_coverage(rows: list[Product]) -> None:
    print("\n=== DATASET CHECK ===")
    print("Rows:", len(rows))
    print("Columns:", ", ".join(OUTPUT_COLUMNS))
    missing_rating = sum(p.rating is None for p in rows)
    missing_price = sum(p.price is None for p in rows)
    missing_brand = sum(not p.brand for p in rows)
    print("Missing rating:", missing_rating)
    print("Missing price:", missing_price)
    print("Missing brand:", missing_brand)
    print("Missing release_year:", sum(p.release_year is None for p in rows))

    for category, brands in REQUESTED_BRANDS.items():
        found = {p.brand for p in rows if p.category == category}
        missing = sorted(b for b in brands if b not in found)
        print(f"{category}: {len(found)} brands seen; requested missing: {missing}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=5000, help="Minimum number of valid rows to collect")
    ap.add_argument("--output", default="electronics_2026_india_5000plus.csv")
    ap.add_argument("--headed", action="store_true", help="Run Chrome visibly")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()

    rows = scrape(target=args.target, headless=not args.headed, pause=args.pause)
    write_csv(rows, args.output)
    report_coverage(rows)
    print(f"\nSaved: {args.output}")

    if len(rows) < args.target:
        print(
            f"WARNING: only {len(rows)} valid rows were collected. No synthetic rows were added. "
            f"Run again with more category sources or a larger browser scroll budget.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()