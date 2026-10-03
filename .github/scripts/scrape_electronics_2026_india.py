#!/usr/bin/env python3
# Smartprix India category sitemaps. Unknown/changed slugs are skipped.
SITEMAP_CATEGORY_SLUGS = {
    "Mobiles": "mobiles",
    "Laptops": "laptops",
    "TVs": "tvs",
    "Headphones": "mobile_headphones",
    "Speakers": "mobile_speakers",
    "Smartwatches": "smartwatches",
    "Tablets": "tablets",
    "Monitors": "monitors",
    "Cameras": "cameras",
    "Power banks": "power_banks",
    "Printers": "printers",
    "Projectors": "projectors",
    "Routers / networking": "routers",
    "Gaming consoles": "gaming_consoles",
    "Microphones": "microphones",
    "Webcams": "webcams",
    "Soundbars": "soundbars",
}
#!/usr/bin/env python3
"""
Collect 5,000+ real electronics products from Smartprix India.

Source strategy:
- Smartprix India XML sitemap index -> product URLs
- Smartprix /ui/api/page-info endpoint -> current product details
- No random/synthetic rows
- Brand comes from Smartprix's structured product data
- Price is the current Smartprix price in INR
- Release year is filled only when an explicit release/launch/availability year exists
"""

import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import cloudscraper


OUTPUT_COLUMNS = ["id", "productname", "brand", "category", "price", "rating", "release_year"]
SMARTPRIX_BASE = "https://www.smartprix.com"
SITEMAP_INDEX = SMARTPRIX_BASE + "/sitemaps/in/sitemap.xml"
API_BASE = SMARTPRIX_BASE + "/ui/api/page-info?k="

CATEGORY_MAP = {
    "mobiles": "Mobiles",
    "laptops": "Laptops",
    "tvs": "TVs",
    "mobile_headphones": "Headphones",
    "headphones": "Headphones",
    "mobile_speakers": "Speakers",
    "speakers": "Speakers",
    "smartwatches": "Smartwatches",
    "tablets": "Tablets",
    "monitors": "Monitors",
    "cameras": "Cameras",
    "power_banks": "Power banks",
    "printers": "Printers",
    "projectors": "Projectors",
    "routers": "Routers / networking",
    "gaming_consoles": "Gaming consoles",
    "microphones": "Microphones",
    "webcams": "Webcams",
    "soundbars": "Soundbars",
}

# Smartprix's encoded-key scheme, used by other Smartprix API scrapers.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"


def fetch_text(session, url: str, timeout: int = 45) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def iter_sitemap_locs(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [
        loc.text.strip()
        for loc in root.findall(".//{*}loc")
        if loc.text and loc.text.strip()
    ]


def encode_api_payload(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    out = "1"
    for ch in text:
        code = ord(ch)
        if code > 127:
            out += ch
            continue
        code %= 95
        if code < 64:
            out += ALPHABET[code]
        else:
            out += "." + ALPHABET[code & 63]
    return out


@dataclass
class Product:
    productname: str
    brand: str
    category: str
    price: Optional[int]
    rating: Optional[float]
    release_year: Optional[int]



BRAND_ALIASES = {
    "oppo": "Oppo", "vivo": "Vivo", "realme": "Realme",
    "oneplus": "OnePlus", "iqoo": "iQOO", "poco": "Poco",
    "boat": "boAt", "boAt": "boAt", "dji": "DJI",
    "hp": "HP", "asus": "Asus", "msi": "MSI", "wd": "WD",
    "tp-link": "TP-Link", "tp link": "TP-Link", "tenda": "Tenda",
    "d-link": "D-Link", "d link": "D-Link", "lg": "LG",
    "jbl": "JBL", "sony": "Sony", "samsung": "Samsung",
    "apple": "Apple", "xiaomi": "Xiaomi", "google": "Google",
    "motorola": "Motorola", "nothing": "Nothing", "honor": "Honor",
    "canon": "Canon", "nikon": "Nikon", "fujifilm": "Fujifilm",
    "panasonic": "Panasonic", "gopro": "GoPro", "lenovo": "Lenovo",
    "dell": "Dell", "acer": "Acer", "microsoft": "Microsoft",
    "tcl": "TCL", "hisense": "Hisense", "vu": "Vu",
    "sandisk": "SanDisk", "kingston": "Kingston", "seagate": "Seagate",
    "crucial": "Crucial", "netgear": "Netgear", "fastrack": "Fastrack",
    "fire-boltt": "Fire-Boltt", "fire boltt": "Fire-Boltt",
    "amazfit": "Amazfit", "garmin": "Garmin", "fossil": "Fossil",
    "logitech": "Logitech", "razer": "Razer", "zebronics": "Zebronics",
    "portronics": "Portronics", "anker": "Anker", "ugreen": "UGREEN",
    "belkin": "Belkin",
}


def normalize_brand_name(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    return BRAND_ALIASES.get(raw, BRAND_ALIASES.get(raw.lower(), raw))

def clean_price(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 0:
            return int(round(value))
        return None

    text = str(value).strip()
    text = text.replace("₹", "").replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None

    try:
        number = float(m.group(0))
    except ValueError:
        return None
    return int(round(number)) if number > 0 else None


def clean_rating(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    else:
        m = re.search(r"\d+(?:\.\d+)?", str(value))
        if not m:
            return None
        x = float(m.group(0))

    if 0 <= x <= 5:
        return round(x, 2)

    # Smartprix structured data commonly exposes its rating as a 0-100 score.
    if 5 < x <= 100:
        return round(x / 20.0, 2)

    return None


def find_rating(item: dict[str, Any]) -> Optional[float]:
    keys = (
        "rating",
        "userRating",
        "user_rating",
        "ratingValue",
        "rating_value",
        "averageRating",
        "average_rating",
    )
    for key in keys:
        if key in item:
            rating = clean_rating(item.get(key))
            if rating is not None:
                return rating

    # Some responses nest review/rating metadata.
    for container_key in ("ratings", "reviews", "reviewSummary", "review_summary"):
        value = item.get(container_key)
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    rating = clean_rating(value.get(key))
                    if rating is not None:
                        return rating
    return None


def explicit_year_from_text(text: str) -> Optional[int]:
    if not text:
        return None

    patterns = [
        r"(?:release|launch|launched|released|introduced|announced|available\s+from|availability)"
        r"[^0-9]{0,80}(20(?:0\d|1\d|2[0-6]))",
        r"(20(?:0\d|1\d|2[0-6]))[^A-Za-z0-9]{0,20}"
        r"(?:release|launch|launched|released|introduced|announced|available\s+from|availability)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            year = int(m.group(1))
            if 2000 <= year <= 2026:
                return year
    return None


def find_release_year(item: dict[str, Any]) -> Optional[int]:
    # Prefer explicit structured release/launch fields from Smartprix.
    direct_keys = (
        "releaseDate", "release_date", "releasedOn", "released_on",
        "launchDate", "launch_date", "availableFrom", "available_from",
        "announcementDate", "announcement_date", "introducedDate", "introduced_date",
    )
    for key in direct_keys:
        if key in item:
            m = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", str(item.get(key) or ""))
            if m:
                return int(m.group(1))

    # Also inspect any key whose name explicitly signals a launch/release date.
    for key, value in item.items():
        low = str(key).lower()
        if any(token in low for token in ("release", "launch", "available", "announced", "introduced")):
            m = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", str(value or ""))
            if m:
                return int(m.group(1))

    full_specs = item.get("fullSpecs", [])
    if isinstance(full_specs, list):
        for group in full_specs:
            if not isinstance(group, dict):
                continue
            group_title = str(group.get("title", ""))
            for spec in group.get("items", []):
                if not isinstance(spec, dict):
                    continue
                title = str(spec.get("title", ""))
                description = str(spec.get("description", ""))
                combined = f"{group_title} {title}: {description}"
                if re.search(r"release|launch|available|announced|introduced", combined, re.I):
                    m = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", combined)
                    if m:
                        return int(m.group(1))
    return None
def fetch_sitemaps(session) -> list[tuple[str, str]]:
    selected = []
    for category, slug in SITEMAP_CATEGORY_SLUGS.items():
        selected.append((category, f"{SMARTPRIX_BASE}/sitemaps/in/{slug}.xml"))
    return selected


def fetch_product_urls(session, per_category_limit: int = 750) -> list[tuple[str, str]]:
    # Return (product_url, category) and interleave categories so shard splits
    # remain category-balanced.
    by_category: dict[str, list[str]] = {}

    for category, sitemap_url in fetch_sitemaps(session):
        try:
            xml_text = fetch_text(session, sitemap_url)
            urls = []
            seen = set()
            for url in iter_sitemap_locs(xml_text):
                path = urlparse(url).path
                if "-ppd" not in path:
                    continue
                clean = url.split("?", 1)[0]
                if clean in seen:
                    continue
                seen.add(clean)
                urls.append(clean)
                if len(urls) >= per_category_limit:
                    break
            by_category[category] = urls
            print(f"[+] {category}: {len(urls)} product URLs", flush=True)
        except Exception as exc:
            print(f"[!] {category} sitemap unavailable: {exc}", file=sys.stderr, flush=True)

    result = []
    categories = list(by_category)
    for i in range(max((len(v) for v in by_category.values()), default=0)):
        for category in categories:
            values = by_category[category]
            if i < len(values):
                result.append((values[i], category))
    print(f"[+] balanced product URLs: {len(result)}", flush=True)
    return result
def category_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    slug = path.split("/", 1)[0].lower()
    return CATEGORY_MAP.get(slug)


def parse_product(data: dict[str, Any], product_url: str) -> Optional[Product]:
    item = data.get("item")
    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip()
    brand_obj = item.get("brand")
    if isinstance(brand_obj, dict):
        brand = normalize_brand_name(brand_obj.get("name"))
    else:
        brand = normalize_brand_name(brand_obj)

    category = category_from_url(product_url)
    if not name or not brand or not category:
        return None

    price = clean_price(item.get("price"))
    if price is None:
        price = clean_price(item.get("currentPrice"))
    if price is None:
        price = clean_price(item.get("fe_price"))

    rating = find_rating(item)
    release_year = find_release_year(item)

    if price is None or rating is None:
        return None

    if release_year is not None and not (2000 <= release_year <= 2026):
        release_year = None

    return Product(
        productname=name,
        brand=brand,
        category=category,
        price=price,
        rating=rating,
        release_year=release_year,
    )


def fetch_product_json(session, product_url: str, max_retries: int = 6) -> Optional[dict[str, Any]]:
    path = urlparse(product_url).path

    for attempt in range(max_retries):
        now_ms = int(time.time() * 1000)
        payload = {
            "url": path,
            "data": {},
            "t": now_ms,
            "st": now_ms - 5000,
        }
        encoded = encode_api_payload(payload)
        api_url = API_BASE + encoded

        try:
            response = session.get(api_url, timeout=45)

            if response.status_code == 429:
                delay = 3 * (attempt + 1)
                print(
                    f"[!] 429 rate limit for {path}; backing off {delay}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None

        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"[!] API error {path}: {exc}", file=sys.stderr, flush=True)
                return None
            delay = 2 * (attempt + 1)
            print(
                f"[!] API retry {attempt + 1}/{max_retries} for {path}: {exc}; "
                f"waiting {delay}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    return None
def scrape(target: int = 1200, pause: float = 1.2, shard: int = 0, shards: int = 1) -> list[Product]:
    session = cloudscraper.create_scraper()
    all_urls = fetch_product_urls(session, per_category_limit=800)

    if not all_urls:
        raise RuntimeError("No Smartprix product URLs were found.")

    shard_urls = all_urls[shard::shards]
    print(
        f"[+] shard {shard + 1}/{shards}: assigned {len(shard_urls)} URLs",
        flush=True,
    )

    products: list[Product] = []
    seen = set()

    for idx, (product_url, sitemap_category) in enumerate(shard_urls, 1):
        data = fetch_product_json(session, product_url)
        if data:
            product = parse_product(data, product_url)
            if product:
                # Use the actual Smartprix sitemap category, not keyword guesses.
                product.category = sitemap_category
                key = (
                    product.productname.casefold(),
                    product.brand.casefold(),
                    product.category.casefold(),
                )
                if key not in seen:
                    seen.add(key)
                    products.append(product)

                    if len(products) % 100 == 0:
                        print(
                            f"[+] shard {shard + 1}: valid rows {len(products)} "
                            f"(processed {idx})",
                            flush=True,
                        )

                    if len(products) >= target:
                        break
        time.sleep(pause)

    return products
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=1200)
    parser.add_argument("--output", default="electronics_shard.csv")
    parser.add_argument("--pause", type=float, default=1.2)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()

    rows = scrape(
        target=args.target,
        pause=args.pause,
        shard=args.shard,
        shards=args.shards,
    )

    write_csv(rows, args.output)

    release_filled = sum(bool(x.release_year) for x in rows)
    print("\n=== SHARD CHECK ===")
    print("Rows:", len(rows))
    print("Brands:", len({x.brand for x in rows}))
    print("Categories:", len({x.category for x in rows}))
    print("Release years filled:", release_filled)
    print("Release years missing:", len(rows) - release_filled)

    if len(rows) < args.target:
        print(
            f"WARNING: shard collected {len(rows)} valid rows; target was {args.target}. "
            "No synthetic rows were added.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
