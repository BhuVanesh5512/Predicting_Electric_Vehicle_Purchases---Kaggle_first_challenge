#!/usr/bin/env python3
"""Collect real Smartprix India electronics products for cross-category analysis.

No random/synthetic rows are generated. Prices are from Smartprix India and are
labelled INR. Release years are filled only from explicit release/launch date
fields or clearly labelled release/launch specs; model numbers and product names
are never treated as years.
"""

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import cloudscraper

SMARTPRIX_BASE = "https://www.smartprix.com"
API_BASE = SMARTPRIX_BASE + "/ui/api/page-info?k="

# Common consumer electronics categories. Missing/retired sitemap endpoints are
# skipped; each collected URL retains the source category it came from.
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
    "Power Banks": "power_banks",
    "Printers": "printers",
    "Projectors": "projectors",
    "Networking": "routers",
    "Gaming Consoles": "gaming_consoles",
    "Microphones": "microphones",
    "Webcams": "webcams",
    "Soundbars": "soundbars",
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
    "power_banks": "Power Banks",
    "printers": "Printers",
    "projectors": "Projectors",
    "routers": "Networking",
    "gaming_consoles": "Gaming Consoles",
    "microphones": "Microphones",
    "webcams": "Webcams",
    "soundbars": "Soundbars",
}

BRAND_ALIASES = {
    "oppo": "Oppo", "vivo": "Vivo", "realme": "Realme", "oneplus": "OnePlus",
    "iqoo": "iQOO", "poco": "Poco", "boat": "boAt", "dji": "DJI", "hp": "HP",
    "asus": "Asus", "msi": "MSI", "wd": "WD", "tp-link": "TP-Link",
    "tp link": "TP-Link", "tenda": "Tenda", "d-link": "D-Link", "d link": "D-Link",
    "lg": "LG", "jbl": "JBL", "sony": "Sony", "samsung": "Samsung", "apple": "Apple",
    "xiaomi": "Xiaomi", "google": "Google", "motorola": "Motorola",
    "nothing": "Nothing", "honor": "Honor", "canon": "Canon", "nikon": "Nikon",
    "fujifilm": "Fujifilm", "panasonic": "Panasonic", "gopro": "GoPro",
    "lenovo": "Lenovo", "dell": "Dell", "acer": "Acer", "microsoft": "Microsoft",
    "tcl": "TCL", "hisense": "Hisense", "vu": "Vu", "sandisk": "SanDisk",
    "kingston": "Kingston", "seagate": "Seagate", "crucial": "Crucial",
    "netgear": "Netgear", "fastrack": "Fastrack", "fire-boltt": "Fire-Boltt",
    "fire boltt": "Fire-Boltt", "amazfit": "Amazfit", "garmin": "Garmin",
    "fossil": "Fossil", "logitech": "Logitech", "razer": "Razer",
    "zebronics": "Zebronics", "portronics": "Portronics", "anker": "Anker",
    "ugreen": "UGREEN", "belkin": "Belkin",
}

# Keep the original columns first for compatibility with an existing notebook;
# add universal, dashboard-friendly fields for provenance and filtering.
OUTPUT_COLUMNS = [
    "id", "productname", "brand", "category", "price", "rating", "release_year",
    "currency", "rating_count", "release_year_source", "product_url", "source",
    "scraped_at", "mrp", "selling_price", "discount_amount", "discount_percent",
    "mrp_source",
]


@dataclass
class Product:
    productname: str
    brand: str
    category: str
    price: int
    mrp: Optional[int]
    mrp_source: str
    rating: Optional[float]
    rating_count: Optional[int]
    release_year: Optional[int]
    release_year_source: str
    product_url: str
    scraped_at: str


def normalize_brand(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    return BRAND_ALIASES.get(raw.casefold(), raw)


def clean_price(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        # Do not silently label foreign-currency text as INR.
        if re.search(r"(?:US\$|\$|USD|EUR|GBP|dollars?)", text, re.I):
            return None
        text = re.sub(r"(?:₹|INR|Rs\.?)", "", text, flags=re.I).replace(",", "").strip()
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            number = float(match.group(0))
        except ValueError:
            return None
    if number <= 0:
        return None
    return int(round(number))




MRP_KEYS = {
    "mrp", "maximumretailprice", "listprice", "originalprice",
    "regularprice", "markedprice", "pricebeforediscount", "wasprice",
    "strikeprice", "retailprice", "maximumretailpriceinr",
}


def _price_from_value(value: Any) -> Optional[int]:
    """Read a monetary value without treating boolean/foreign-currency text as INR."""
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "displayValue", "display_value", "text"):
            if key in value:
                parsed = clean_price(value.get(key))
                if parsed is not None:
                    return parsed
        return None
    return clean_price(value)


def find_mrp(item: dict[str, Any], selling_price: int) -> tuple[Optional[int], str]:
    """Extract an explicit MRP/list/original price; never infer it from a discount."""
    candidates: list[tuple[int, str]] = []

    def walk(value: Any, path: str = "item", depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                child_path = f"{path}.{key}"
                if normalized in MRP_KEYS:
                    amount = _price_from_value(child)
                    if amount is not None:
                        candidates.append((amount, child_path))
                if isinstance(child, (dict, list)):
                    walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[{index}]", depth + 1)

    walk(item)

    # Some product detail payloads put MRP in a named specification rather than
    # a dedicated price field. Only accept a value where the specification label
    # explicitly says MRP/list/maximum retail price.
    full_specs = item.get("fullSpecs", [])
    if isinstance(full_specs, list):
        for group in full_specs:
            if not isinstance(group, dict):
                continue
            for spec in group.get("items", []) or []:
                if not isinstance(spec, dict):
                    continue
                title = re.sub(r"\s+", " ", str(spec.get("title") or "")).strip()
                if re.search(r"\b(?:mrp|maximum retail price|list price|marked price)\b", title, re.I):
                    for field in ("description", "value", "text"):
                        amount = _price_from_value(spec.get(field))
                        if amount is not None:
                            candidates.append((amount, f"item.fullSpecs.{title}"))

    # Reject values below the selling price because such a value is not a valid
    # original/list price for an item currently sold at the detected price.
    valid = [(amount, source) for amount, source in candidates if amount >= selling_price]
    if not valid:
        return None, ""
    # Prefer the most explicitly named field and avoid arbitrarily taking a
    # giant numeric from an unrelated specification.
    priority = {
        "mrp": 0, "maximumretailprice": 1, "maximumretailpriceinr": 2,
        "originalprice": 3, "listprice": 4, "pricebeforediscount": 5,
        "regularprice": 6, "markedprice": 7, "wasprice": 8,
        "strikeprice": 9, "retailprice": 10,
    }
    def rank(candidate: tuple[int, str]) -> tuple[int, int]:
        amount, source = candidate
        key = re.sub(r"[^a-z0-9]", "", source.rsplit(".", 1)[-1].split("[", 1)[0].casefold())
        return (priority.get(key, 50), amount)
    amount, source = sorted(valid, key=rank)[0]
    return amount, source


def clean_rating(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(value))
        if not match:
            return None
        number = float(match.group(0))
    if 0 <= number <= 5:
        return round(number, 2)
    # Some Smartprix fields are expressed as a score out of 100.
    if 5 < number <= 100:
        return round(number / 20.0, 2)
    return None


def find_rating(item: dict[str, Any]) -> Optional[float]:
    keys = (
        "rating", "userRating", "user_rating", "ratingValue", "rating_value",
        "averageRating", "average_rating",
    )
    containers = [item.get(k) for k in (
        "ratings", "reviews", "reviewSummary", "review_summary", "ratingSummary"
    )]
    for key in keys:
        if key in item:
            value = clean_rating(item.get(key))
            if value is not None:
                return value
    for container in containers:
        if isinstance(container, dict):
            for key in keys:
                if key in container:
                    value = clean_rating(container.get(key))
                    if value is not None:
                        return value
    return None


def find_rating_count(item: dict[str, Any]) -> Optional[int]:
    keys = (
        "ratingCount", "rating_count", "reviewCount", "review_count",
        "reviewsCount", "reviews_count", "totalRatings", "total_ratings",
        "ratingsCount", "ratings_count", "userRatingCount",
    )
    containers = [item.get(k) for k in (
        "ratings", "reviews", "reviewSummary", "review_summary", "ratingSummary"
    )]
    for obj in [item] + [c for c in containers if isinstance(c, dict)]:
        for key in keys:
            if key not in obj:
                continue
            value = obj.get(key)
            if isinstance(value, bool):
                continue
            match = re.search(r"\d+", str(value or "").replace(",", ""))
            if match:
                count = int(match.group(0))
                if count >= 0:
                    return count
    return None


def extract_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"\b(20\d{2})\b", text)
    if not match:
        return None
    year = int(match.group(1))
    current_year = datetime.now(timezone.utc).year
    return year if 2000 <= year <= current_year else None


def find_release_year(item: dict[str, Any]) -> tuple[Optional[int], str]:
    # Only allow fields whose meaning explicitly states release/launch timing.
    direct_keys = (
        "releaseDate", "release_date", "releaseYear", "release_year",
        "releasedOn", "released_on", "launchDate", "launch_date", "launchYear",
        "launch_year", "launchedOn", "launched_on", "announcementDate",
        "announcement_date", "introducedDate", "introduced_date",
        "availableFrom", "available_from",
    )
    for key in direct_keys:
        if key in item:
            year = extract_year(item.get(key))
            if year is not None:
                return year, f"Smartprix field: {key}"

    # Look at detailed specs only when the spec label itself names a release,
    # launch, announcement or availability date/year. Never parse a model name,
    # SKU, manufacturing date, review date or a generic number as a release year.
    full_specs = item.get("fullSpecs", [])
    if isinstance(full_specs, list):
        label_pattern = re.compile(
            r"^\s*(?:(?:india|global)\s+)?(?:date\s+of\s+)?"
            r"(?:release|released|launch|launched|announcement|announced|introduced|"
            r"availability|available)\s*(?:date|year|on|from|in india|in india date)?\s*$",
            re.I,
        )
        reverse_pattern = re.compile(
            r"^\s*(?:release|launch|announcement|introduced)\s+(?:date|year)"
            r"(?:\s*\([^)]*\))?\s*$",
            re.I,
        )
        for group in full_specs:
            if not isinstance(group, dict):
                continue
            for spec in group.get("items", []) or []:
                if not isinstance(spec, dict):
                    continue
                title = re.sub(r"\s+", " ", str(spec.get("title") or "")).strip()
                if not (label_pattern.match(title) or reverse_pattern.match(title)):
                    continue
                for field in ("description", "value", "text"):
                    year = extract_year(spec.get(field))
                    if year is not None:
                        return year, f"Smartprix specification: {title}"
    return None, ""


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


def fetch_product_urls(session, per_category_limit: int = 1800) -> list[tuple[str, str]]:
    by_category: dict[str, list[str]] = {}
    for category, slug in SITEMAP_CATEGORY_SLUGS.items():
        sitemap_url = f"{SMARTPRIX_BASE}/sitemaps/in/{slug}.xml"
        try:
            xml_text = fetch_text(session, sitemap_url)
            urls, seen = [], set()
            for url in iter_sitemap_locs(xml_text):
                parsed = urlparse(url)
                if "smartprix.com" not in parsed.netloc.lower():
                    continue
                if "-ppd" not in parsed.path:
                    continue
                clean = url.split("?", 1)[0].rstrip("/")
                if clean in seen:
                    continue
                seen.add(clean)
                urls.append(clean)
                if len(urls) >= per_category_limit:
                    break
            if urls:
                by_category[category] = urls
            print(f"[+] {category}: {len(urls)} product URLs", flush=True)
        except Exception as exc:
            print(f"[!] {category} sitemap unavailable: {exc}", file=sys.stderr, flush=True)

    output: list[tuple[str, str]] = []
    categories = list(by_category)
    for idx in range(max((len(urls) for urls in by_category.values()), default=0)):
        for category in categories:
            if idx < len(by_category[category]):
                output.append((by_category[category][idx], category))
    print(f"[+] total URLs across categories: {len(output)}; active categories: {len(by_category)}", flush=True)
    return output


def fetch_product_json(session, product_url: str, max_retries: int = 8) -> Optional[dict[str, Any]]:
    path = urlparse(product_url).path
    for attempt in range(max_retries):
        now_ms = int(time.time() * 1000)
        payload = {"url": path, "data": {}, "t": now_ms, "st": now_ms - 5000}
        api_url = API_BASE + encode_api_payload(payload)
        try:
            response = session.get(api_url, timeout=45)
            if response.status_code == 429:
                delay = min(30, 5 * (attempt + 1))
                print(f"[!] rate limited for {path}; retrying in {delay}s", file=sys.stderr, flush=True)
                time.sleep(delay)
                continue
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"[!] API error for {path}: {exc}", file=sys.stderr, flush=True)
                return None
            delay = min(20, 3 * (attempt + 1))
            time.sleep(delay)
    return None


def parse_product(data: dict[str, Any], category: str, product_url: str) -> Optional[Product]:
    item = data.get("item")
    if not isinstance(item, dict):
        return None

    name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
    brand_obj = item.get("brand")
    if isinstance(brand_obj, dict):
        brand = normalize_brand(brand_obj.get("name"))
    else:
        brand = normalize_brand(brand_obj)

    # The India endpoint is expected to return INR amounts. Currency-like text
    # that explicitly says USD/EUR/GBP is rejected instead of relabelled INR.
    price = clean_price(item.get("price"))
    if price is None:
        price = clean_price(item.get("currentPrice"))
    if price is None:
        price = clean_price(item.get("fe_price"))
    if not name or not brand or price is None:
        return None

    mrp, mrp_source = find_mrp(item, price)
    release_year, release_year_source = find_release_year(item)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return Product(
        productname=name,
        brand=brand,
        category=category,
        price=price,
        mrp=mrp,
        mrp_source=mrp_source,
        rating=find_rating(item),
        rating_count=find_rating_count(item),
        release_year=release_year,
        release_year_source=release_year_source,
        product_url=product_url,
        scraped_at=now,
    )


def scrape(target: int, pause: float, shard: int, shards: int) -> list[Product]:
    if shards < 1 or not 0 <= shard < shards:
        raise ValueError("shard must be in [0, shards), and shards must be >= 1")
    session = cloudscraper.create_scraper()
    all_urls = fetch_product_urls(session, per_category_limit=1800)
    if not all_urls:
        raise RuntimeError("No Smartprix product URLs were found.")
    assigned = all_urls[shard::shards]
    print(f"[+] shard {shard + 1}/{shards}: {len(assigned)} assigned URLs", flush=True)

    products: list[Product] = []
    seen = set()
    for idx, (url, category) in enumerate(assigned, 1):
        data = fetch_product_json(session, url)
        if data:
            product = parse_product(data, category, url)
            if product:
                key = (
                    re.sub(r"[^a-z0-9]+", "", product.productname.casefold()),
                    re.sub(r"[^a-z0-9]+", "", product.brand.casefold()),
                )
                if key not in seen:
                    seen.add(key)
                    products.append(product)
                    if len(products) % 100 == 0:
                        print(f"[+] shard {shard + 1}: rows={len(products)} processed={idx}", flush=True)
                    if len(products) >= target:
                        break
        time.sleep(pause)

    return products


def write_csv(rows: list[Product], output: str) -> None:
    with open(output, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for idx, p in enumerate(rows, 1):
            writer.writerow({
                "id": idx,
                "productname": p.productname,
                "brand": p.brand,
                "category": p.category,
                # price stays the observed Smartprix selling price for backwards
                # compatibility. The MRP is a separate explicit-source column.
                "price": p.price,
                "selling_price": p.price,
                "mrp": "" if p.mrp is None else p.mrp,
                "discount_amount": "" if p.mrp is None or p.mrp <= p.price else p.mrp - p.price,
                "discount_percent": "" if p.mrp is None or p.mrp <= 0 else round((p.mrp - p.price) * 100 / p.mrp, 2),
                "mrp_source": p.mrp_source,
                "rating": "" if p.rating is None else p.rating,
                "release_year": "" if p.release_year is None else p.release_year,
                "currency": "INR",
                "rating_count": "" if p.rating_count is None else p.rating_count,
                "release_year_source": p.release_year_source,
                "product_url": p.product_url,
                "source": "Smartprix India",
                "scraped_at": p.scraped_at,
            })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pause", type=float, default=1.2)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()

    rows = scrape(args.target, args.pause, args.shard, args.shards)
    write_csv(rows, args.output)
    print(f"[+] ROWS: {len(rows)}", flush=True)
    print(f"[+] BRANDS: {len({p.brand for p in rows})}", flush=True)
    print(f"[+] CATEGORIES: {len({p.category for p in rows})}", flush=True)
    print(f"[+] RELEASE YEARS FILLED: {sum(p.release_year is not None for p in rows)}", flush=True)
    if len(rows) < args.target:
        print(f"ERROR: only {len(rows)} valid rows; target={args.target}; no synthetic rows added.",
              file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
