#!/usr/bin/env python3
"""
Real Smartprix India electronics scraper.

Outputs:
  id, productname, brand, category, price, rating, release_year

Rules:
- Real Smartprix product URLs from Smartprix India category sitemaps.
- Current Smartprix product price.
- Brand from Smartprix structured product data.
- Category from the sitemap/category source.
- Rating from Smartprix structured rating data.
- Release year only when explicitly published by Smartprix.
- No random or synthetic rows.
"""

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import cloudscraper


OUTPUT_COLUMNS = [
    "id",
    "productname",
    "brand",
    "category",
    "price",
    "rating",
    "release_year",
]

SMARTPRIX_BASE = "https://www.smartprix.com"
API_BASE = SMARTPRIX_BASE + "/ui/api/page-info?k="

# These are Smartprix India sitemap/category slugs known to expose product URLs.
SITEMAP_CATEGORY_SLUGS = {
    "Mobiles": "mobiles",
    "Laptops": "laptops",
    "TVs": "tvs",
    "Headphones": "mobile_headphones",
    "Smartwatches": "smartwatches",
    "Tablets": "tablets",
    "Monitors": "monitors",
    "Cameras": "cameras",
    "Speakers": "speakers",
}

CATEGORY_BY_PATH = {
    "mobiles": "Mobiles",
    "laptops": "Laptops",
    "tvs": "TVs",
    "mobile_headphones": "Headphones",
    "headphones": "Headphones",
    "smartwatches": "Smartwatches",
    "tablets": "Tablets",
    "monitors": "Monitors",
    "cameras": "Cameras",
    "mobile_speakers": "Speakers",
    "speakers": "Speakers",
}

BRAND_ALIASES = {
    "oppo": "Oppo",
    "vivo": "Vivo",
    "realme": "Realme",
    "oneplus": "OnePlus",
    "iqoo": "iQOO",
    "poco": "Poco",
    "boat": "boAt",
    "dji": "DJI",
    "hp": "HP",
    "asus": "Asus",
    "msi": "MSI",
    "wd": "WD",
    "tp-link": "TP-Link",
    "tp link": "TP-Link",
    "tenda": "Tenda",
    "d-link": "D-Link",
    "d link": "D-Link",
    "lg": "LG",
    "jbl": "JBL",
    "sony": "Sony",
    "samsung": "Samsung",
    "apple": "Apple",
    "xiaomi": "Xiaomi",
    "google": "Google",
    "motorola": "Motorola",
    "nothing": "Nothing",
    "honor": "Honor",
    "canon": "Canon",
    "nikon": "Nikon",
    "fujifilm": "Fujifilm",
    "panasonic": "Panasonic",
    "gopro": "GoPro",
    "lenovo": "Lenovo",
    "dell": "Dell",
    "acer": "Acer",
    "microsoft": "Microsoft",
    "tcl": "TCL",
    "hisense": "Hisense",
    "vu": "Vu",
    "sandisk": "SanDisk",
    "kingston": "Kingston",
    "seagate": "Seagate",
    "crucial": "Crucial",
    "netgear": "Netgear",
    "fastrack": "Fastrack",
    "fire-boltt": "Fire-Boltt",
    "fire boltt": "Fire-Boltt",
    "amazfit": "Amazfit",
    "garmin": "Garmin",
    "fossil": "Fossil",
    "logitech": "Logitech",
    "razer": "Razer",
    "zebronics": "Zebronics",
    "portronics": "Portronics",
    "anker": "Anker",
    "ugreen": "UGREEN",
    "belkin": "Belkin",
}


@dataclass
class Product:
    productname: str
    brand: str
    category: str
    price: int
    rating: float
    release_year: Optional[int]


def normalize_brand(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    return BRAND_ALIASES.get(raw.lower(), raw)


def clean_price(value: Any) -> Optional[int]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        n = int(round(float(value)))
        return n if n > 0 else None

    text = str(value).replace("₹", "").replace(",", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        n = int(round(float(match.group(0))))
    except ValueError:
        return None
    return n if n > 0 else None


def clean_rating(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        x = float(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(value))
        if not match:
            return None
        x = float(match.group(0))

    # Preserve normal 0-5 ratings.
    if 0 <= x <= 5:
        return round(x, 2)

    # Smartprix commonly exposes ratings as percentages/scores out of 100.
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

    for container_key in ("ratings", "reviews", "reviewSummary", "review_summary"):
        value = item.get(container_key)
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    rating = clean_rating(value.get(key))
                    if rating is not None:
                        return rating

    return None


def find_release_year(item: dict[str, Any]) -> Optional[int]:
    direct_keys = (
        "releaseDate",
        "release_date",
        "releasedOn",
        "released_on",
        "launchDate",
        "launch_date",
        "availableFrom",
        "available_from",
        "announcementDate",
        "announcement_date",
        "introducedDate",
        "introduced_date",
    )

    for key in direct_keys:
        match = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", str(item.get(key) or ""))
        if match:
            return int(match.group(1))

    for key, value in item.items():
        low = str(key).lower()
        if any(token in low for token in ("release", "launch", "available", "announced", "introduced")):
            match = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", str(value or ""))
            if match:
                return int(match.group(1))

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
                    match = re.search(r"\b(20(?:0\d|1\d|2[0-6]))\b", combined)
                    if match:
                        return int(match.group(1))

    return None


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
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    out = "1"

    for ch in text:
        code = ord(ch)
        if code > 127:
            out += ch
            continue

        code %= 95
        if code < 64:
            out += alphabet[code]
        else:
            out += "." + alphabet[code & 63]

    return out


def category_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.strip("/")
    if not path:
        return None

    first = path.split("/", 1)[0].lower()
    return CATEGORY_BY_PATH.get(first)


def fetch_product_urls(session, per_category_limit: int = 1800) -> list[tuple[str, str]]:
    by_category: dict[str, list[str]] = {}

    for category, slug in SITEMAP_CATEGORY_SLUGS.items():
        sitemap_url = f"{SMARTPRIX_BASE}/sitemaps/in/{slug}.xml"

        try:
            xml_text = fetch_text(session, sitemap_url)
            urls = []
            seen = set()

            for url in iter_sitemap_locs(xml_text):
                if "-ppd" not in urlparse(url).path:
                    continue

                clean = url.split("?", 1)[0]

                if clean in seen:
                    continue

                seen.add(clean)
                urls.append(clean)

                if len(urls) >= per_category_limit:
                    break

            by_category[category] = urls
            print(f"[+] {category}: {len(urls)} URLs", flush=True)

        except Exception as exc:
            print(f"[!] {category} sitemap unavailable: {exc}", file=sys.stderr, flush=True)

    # Interleave categories for better coverage per shard.
    output = []
    categories = list(by_category)

    max_len = max((len(v) for v in by_category.values()), default=0)
    for i in range(max_len):
        for category in categories:
            values = by_category[category]
            if i < len(values):
                output.append((values[i], category))

    print(f"[+] total product URLs: {len(output)}", flush=True)
    return output


def fetch_product_json(session, product_url: str, max_retries: int = 8) -> Optional[dict[str, Any]]:
    path = urlparse(product_url).path

    for attempt in range(max_retries):
        now_ms = int(time.time() * 1000)
        payload = {
            "url": path,
            "data": {},
            "t": now_ms,
            "st": now_ms - 5000,
        }

        api_url = API_BASE + encode_api_payload(payload)

        try:
            response = session.get(api_url, timeout=45)

            if response.status_code == 429:
                delay = min(30, 5 * (attempt + 1))
                print(
                    f"[!] 429 for {path}; retrying in {delay}s",
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

            delay = min(20, 3 * (attempt + 1))
            print(
                f"[!] API retry for {path}: {exc}; sleeping {delay}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    return None


def parse_product(data: dict[str, Any], sitemap_category: str) -> Optional[Product]:
    item = data.get("item")

    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip()

    brand_obj = item.get("brand")
    if isinstance(brand_obj, dict):
        brand = normalize_brand(brand_obj.get("name"))
    else:
        brand = normalize_brand(brand_obj)

    price = clean_price(item.get("price"))
    if price is None:
        price = clean_price(item.get("currentPrice"))
    if price is None:
        price = clean_price(item.get("fe_price"))

    rating = find_rating(item)

    if not name or not brand or price is None or rating is None:
        return None

    release_year = find_release_year(item)
    if release_year is not None and not 2000 <= release_year <= 2026:
        release_year = None

    return Product(
        productname=name,
        brand=brand,
        category=sitemap_category,
        price=price,
        rating=rating,
        release_year=release_year,
    )


def scrape(target: int, pause: float, shard: int, shards: int) -> list[Product]:
    session = cloudscraper.create_scraper()

    all_urls = fetch_product_urls(session, per_category_limit=1800)
    assigned = all_urls[shard::shards]

    print(
        f"[+] shard {shard + 1}/{shards}: {len(assigned)} assigned URLs",
        flush=True,
    )

    products: list[Product] = []
    seen = set()

    # Give each shard enough candidate URLs to reach its quota.
    for idx, (url, category) in enumerate(assigned, 1):
        data = fetch_product_json(session, url)

        if data:
            product = parse_product(data, category)

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
                            f"[+] shard {shard + 1}: valid rows={len(products)} "
                            f"processed={idx}",
                            flush=True,
                        )

                    if len(products) >= target:
                        break

        time.sleep(pause)

    return products


def write_csv(rows: list[Product], output: str) -> None:
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--output", required=True)
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

    print(f"[+] SHARD ROWS: {len(rows)}", flush=True)
    print(f"[+] BRANDS: {len({x.brand for x in rows})}", flush=True)
    print(f"[+] CATEGORIES: {len({x.category for x in rows})}", flush=True)
    print(
        f"[+] RELEASE YEARS FILLED: {sum(x.release_year is not None for x in rows)}",
        flush=True,
    )

    if len(rows) < args.target:
        print(
            f"ERROR: only {len(rows)} valid rows collected; target={args.target}. "
            "No synthetic rows were added.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
