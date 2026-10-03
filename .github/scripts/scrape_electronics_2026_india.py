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
    # Check obvious scalar fields first.
    for key, value in item.items():
        low = str(key).lower()
        if any(token in low for token in ("release", "launch", "available", "announced", "introduced")):
            year = explicit_year_from_text(str(value))
            if year is not None:
                return year

    # Search structured specs, but require an explicit release/launch/availability cue.
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
                    year = explicit_year_from_text(combined)
                    if year is not None:
                        return year

    # Do not infer a year just because the model name contains one.
    return None


def category_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    slug = path.split("/", 1)[0].lower()
    return CATEGORY_MAP.get(slug)


def iter_sitemap_locs(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [
        loc.text.strip()
        for loc in root.findall(".//{*}loc")
        if loc.text and loc.text.strip()
    ]


def fetch_text(session, url: str, timeout: int = 45) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_sitemaps(session) -> list[str]:
    index_text = fetch_text(session, SITEMAP_INDEX)
    child_urls = iter_sitemap_locs(index_text)

    # Prefer electronics/product sitemaps; ignore news/bytes assets.
    selected = []
    for url in child_urls:
        low = url.lower()
        if "/bytes/" in low:
            continue
        if any(token in low for token in CATEGORY_MAP):
            selected.append(url)

    if not selected:
        # Fall back to all child sitemaps except bytes/news.
        selected = [u for u in child_urls if "/bytes/" not in u.lower()]

    print(f"[+] sitemap children discovered: {len(child_urls)}", flush=True)
    print(f"[+] sitemap children selected: {len(selected)}", flush=True)
    return selected


def fetch_product_urls(session, limit: int = 6500) -> list[str]:
    product_urls = []
    seen = set()

    for sitemap_url in fetch_sitemaps(session):
        try:
            xml_text = fetch_text(session, sitemap_url)
            for url in iter_sitemap_locs(xml_text):
                category = category_from_url(url)
                if category is None:
                    continue

                path = urlparse(url).path
                if "-ppd" not in path:
                    continue

                clean = url.split("?", 1)[0]
                if clean in seen:
                    continue
                seen.add(clean)
                product_urls.append(clean)

                if len(product_urls) >= limit:
                    break

            print(f"[+] {sitemap_url}: total unique product URLs = {len(product_urls)}", flush=True)
            if len(product_urls) >= limit:
                break
        except Exception as exc:
            print(f"[!] sitemap error {sitemap_url}: {exc}", file=sys.stderr, flush=True)

    return product_urls


def fetch_product_json(session, product_url: str) -> Optional[dict[str, Any]]:
    path = urlparse(product_url).path
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
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"[!] API error {path}: {exc}", file=sys.stderr, flush=True)
        return None


def parse_product(data: dict[str, Any], product_url: str) -> Optional[Product]:
    item = data.get("item")
    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip()
    brand_obj = item.get("brand")
    brand = ""

    if isinstance(brand_obj, dict):
        brand = str(brand_obj.get("name") or "").strip()
    elif brand_obj:
        brand = str(brand_obj).strip()

    category = category_from_url(product_url)
    if not name or not brand or not category:
        return None

    price = clean_price(item.get("price"))
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


def scrape(target: int = 5000, pause: float = 0.35) -> list[Product]:
    session = cloudscraper.create_scraper()
    product_urls = fetch_product_urls(session, limit=max(6500, target + 1000))

    if not product_urls:
        raise RuntimeError("No Smartprix product URLs were found in the sitemap.")

    products = []
    seen = set()

    for idx, product_url in enumerate(product_urls, 1):
        data = fetch_product_json(session, product_url)
        if data:
            product = parse_product(data, product_url)
            if product:
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
                            f"[+] valid rows: {len(products)} "
                            f"(processed URLs: {idx})",
                            flush=True,
                        )

                    if len(products) >= target:
                        break

        time.sleep(pause)

    return products


def write_csv(rows: Iterable[Product], output: str) -> None:
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_COLUMNS)

        for idx, product in enumerate(rows, 1):
            writer.writerow([
                idx,
                product.productname,
                product.brand,
                product.category,
                product.price,
                product.rating,
                product.release_year if product.release_year is not None else "",
            ])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=5000)
    parser.add_argument("--output", default="electronics_2026_india_5000plus.csv")
    parser.add_argument("--pause", type=float, default=0.35)
    args = parser.parse_args()

    rows = scrape(target=args.target, pause=args.pause)

    if len(rows) < args.target:
        print(
            f"WARNING: only {len(rows)} valid real Smartprix products were collected. "
            f"No synthetic rows were added.",
            file=sys.stderr,
        )
        # Write whatever was genuinely collected for diagnostics.
        write_csv(rows, args.output)
        sys.exit(2)

    write_csv(rows, args.output)

    release_filled = sum(bool(x.release_year) for x in rows)
    brands = len({x.brand for x in rows})
    categories = len({x.category for x in rows})

    print("\n=== FINAL DATASET CHECK ===")
    print("Rows:", len(rows))
    print("Brands:", brands)
    print("Categories:", categories)
    print("Release years filled:", release_filled)
    print("Release years missing:", len(rows) - release_filled)
    print("Price currency/source: INR / current Smartprix product API")
    print("First row:", rows[0])
    print("Last row:", rows[-1])


if __name__ == "__main__":
    main()
