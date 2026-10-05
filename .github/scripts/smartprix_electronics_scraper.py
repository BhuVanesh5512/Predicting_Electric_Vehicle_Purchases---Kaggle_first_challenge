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
from urllib.parse import urlparse, urlunparse, parse_qs, unquote_plus, quote_plus

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
    "mrp_source", "mrp_source_type", "mrp_verified_at",
]


@dataclass
class Product:
    productname: str
    brand: str
    category: str
    price: int
    mrp: Optional[int]
    mrp_source: str
    mrp_source_type: str
    mrp_verified_at: str
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


# Price verification sources. Prefer the product manufacturer's own regional
# website, then known Indian retail chains/marketplaces. Wikipedia and blogs
# are deliberately excluded as sources of current MRP.
OFFICIAL_DOMAINS = {
    "samsung": ("samsung.com",),
    "apple": ("apple.com",),
    "lenovo": ("lenovo.com",),
    "hp": ("hp.com",),
    "dell": ("dell.com",),
    "asus": ("asus.com",),
    "acer": ("acer.com",),
    "msi": ("msi.com",),
    "lg": ("lg.com",),
    "sony": ("sony.co.in", "sony.com"),
    "xiaomi": ("mi.com", "xiaomi.com"),
    "realme": ("realme.com",),
    "oneplus": ("oneplus.in", "oneplus.com"),
    "oppo": ("oppo.com",),
    "vivo": ("vivo.com",),
    "motorola": ("motorola.com", "motorola.in"),
    "nothing": ("nothing.tech",),
    "google": ("store.google.com", "google.com"),
    "canon": ("canon.co.in", "canon.com"),
    "nikon": ("nikon.co.in", "nikon.com"),
    "fujifilm": ("fujifilm.com", "fujifilm-x.com"),
    "panasonic": ("panasonic.com",),
    "gopro": ("gopro.com",),
    "tcl": ("tcl.com",),
    "hisense": ("hisense-india.com", "hisense.com"),
    "boat": ("boat-lifestyle.com",),
    "jbl": ("in.jbl.com", "jbl.com"),
    "logitech": ("logitech.com",),
    "razer": ("razer.com",),
    "tp-link": ("tp-link.com",),
    "netgear": ("netgear.com",),
    "wd": ("westerndigital.com",),
    "seagate": ("seagate.com",),
    "kingston": ("kingston.com",),
    "sandisk": ("sandisk.com",),
    "fire-boltt": ("fireboltt.com",),
    "amazfit": ("amazfit.com",),
    "garmin": ("garmin.com",),
    "fastrack": ("fastrack.in",),
    "noise": ("gonoise.com",),
    "portronics": ("portronics.com",),
    "zebronics": ("zebronics.com",),
    "anker": ("anker.com",),
    "ugreen": ("ugreen.com",),
    "belkin": ("belkin.com",),
    "fossil": ("fossil.com",),
    "crucial": ("crucial.com",),
    "d-link": ("dlink.com",),
    "tenda": ("tendacn.com", "tenda.com"),
    "vu": ("vutvs.com",),
}
RETAILER_DOMAINS = (
    "amazon.in", "flipkart.com", "croma.com", "reliancedigital.in",
    "vijaysales.com", "tatacliq.com", "shopatsc.com", "poorvika.com",
    "sangeethamobiles.com", "bajajmall.in",
)


def _domain_matches(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    host = (host or "").casefold().split(":")[0]
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _canonical_result_url(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    if parsed.netloc.lower().endswith("duckduckgo.com"):
        query = parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        if target:
            return unquote_plus(target)
    return href


def _extract_explicit_mrp(text: str, selling_price: int) -> Optional[int]:
    """Only return a number immediately labelled as MRP/list retail price."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text)
    patterns = (
        r"(?:MRP|M\.?R\.?P\.?|maximum\s+retail\s+price|list\s+price|marked\s+price)"
        r"\s*(?:is|:|\-|–|₹|Rs\.?|INR)?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)\s*(?:\([^)]{0,15}\)\s*)?"
        r"(?:MRP|M\.?R\.?P\.?|maximum\s+retail\s+price)",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.I)
        if not match:
            continue
        amount = clean_price(match.group(1))
        if amount is not None and amount >= selling_price:
            return amount
    return None


def _result_matches_product(product_name: str, title: str, snippet: str) -> bool:
    # Quoted product-name query provides the main disambiguation; additionally
    # require a few meaningful model tokens to avoid accepting a sibling model.
    text = f"{title} {snippet}".casefold()
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", product_name.casefold())
        if len(t) >= 3 and t not in {
            "with", "from", "inch", "inches", "black", "white", "blue", "silver",
            "grey", "gray", "gold", "ram", "rom", "wifi", "wi-fi", "gb", "tb",
            "inch", "full", "hdmi", "display", "smart", "wireless", "android",
            "edition", "series", "tablet", "laptop", "monitor", "headphones",
            "earbuds", "camera", "mobile", "smartphone", "television", "led",
        }
    ]
    model_tokens = [t for t in tokens if any(ch.isdigit() for ch in t)]
    if model_tokens:
        return sum(token in text for token in model_tokens) >= min(2, len(model_tokens))
    useful = [t for t in tokens if len(t) >= 5]
    return bool(useful) and sum(token in text for token in useful) >= min(2, len(useful))


def verify_mrp_online(
    session,
    brand: str,
    product_name: str,
    selling_price: int,
) -> tuple[Optional[int], str, str, str]:
    """Verify MRP against one official/retailer product page.

    The search is only discovery; we only accept an explicitly labelled MRP
    from the matching product page itself. There is exactly one search and at
    most one page request per product. Wikipedia, launch prices and discounts
    are not used as MRP evidence.
    """
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    brand_key = brand.casefold()
    official = OFFICIAL_DOMAINS.get(brand_key, ())
    query = f'"{product_name}" MRP India'
    search_url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)

    try:
        response = session.get(search_url, timeout=8)
        response.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"[!] MRP search unavailable for {product_name[:70]}: {exc}", file=sys.stderr, flush=True)
        return None, "", "unavailable", checked_at

    candidates = []
    for result in soup.select(".result"):
        anchor = result.select_one("a.result__a")
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        source_url = _canonical_result_url(str(anchor.get("href") or ""))
        snippet_node = result.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        parsed = urlparse(source_url)
        host = parsed.hostname or ""

        if official and _domain_matches(host, official):
            source_type = "manufacturer_official"
            priority = 0
        elif _domain_matches(host, RETAILER_DOMAINS):
            source_type = "retailer"
            priority = 1
        else:
            continue
        if not source_url or not _result_matches_product(product_name, title, snippet):
            continue
        candidates.append((priority, source_url, source_type, title))

    # Only one matching page gets opened. This bounds the lookup time and
    # avoids letting several unrelated search results stall a shard for hours.
    candidates.sort(key=lambda result: result[0])
    if not candidates:
        return None, "", "not_found", checked_at

    _, source_url, source_type, _ = candidates[0]
    try:
        page = session.get(source_url, timeout=8)
        if page.status_code >= 400:
            return None, "", "unavailable", checked_at

        # Do not accept MRP from a page that redirects away from an allowed
        # manufacturer/retailer domain.
        final_url = str(getattr(page, "url", source_url) or source_url)
        final_host = urlparse(final_url).hostname or ""
        allowed = official if source_type == "manufacturer_official" else RETAILER_DOMAINS
        if not _domain_matches(final_host, allowed):
            final_url, final_host = source_url, urlparse(source_url).hostname or ""
            if not _domain_matches(final_host, allowed):
                return None, "", "unavailable", checked_at

        from bs4 import BeautifulSoup
        page_soup = BeautifulSoup(page.text, "html.parser")
        for node in page_soup(["script", "style", "noscript", "svg"]):
            node.decompose()
        page_text = page_soup.get_text(" ", strip=True)
        amount = _extract_explicit_mrp(page_text, selling_price)
        if amount is not None:
            return amount, final_url, source_type, checked_at
    except Exception as exc:
        print(f"[!] MRP page check unavailable for {product_name[:70]}: {exc}", file=sys.stderr, flush=True)

    return None, "", "no_explicit_mrp", checked_at

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
    local_mrp_source_type = "smartprix_explicit" if mrp is not None else "unavailable"
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return Product(
        productname=name,
        brand=brand,
        category=category,
        price=price,
        mrp=mrp,
        mrp_source=mrp_source,
        mrp_source_type=local_mrp_source_type,
        mrp_verified_at=checked_at,
        rating=find_rating(item),
        rating_count=find_rating_count(item),
        release_year=release_year,
        release_year_source=release_year_source,
        product_url=product_url,
        scraped_at=now,
    )


def scrape(target: int, pause: float, shard: int, shards: int, mrp_verification_limit: int = 200) -> list[Product]:
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
    mrp_checks = 0
    mrp_found = 0
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
                    if product.mrp is None and mrp_checks < mrp_verification_limit:
                        mrp_checks += 1
                        external_mrp, source_url, source_type, checked_at = verify_mrp_online(
                            session, product.brand, product.productname, product.price
                        )
                        product.mrp = external_mrp
                        product.mrp_source = source_url
                        product.mrp_source_type = source_type
                        product.mrp_verified_at = checked_at
                        if external_mrp is not None:
                            mrp_found += 1
                    elif product.mrp is None:
                        product.mrp_source_type = "not_checked_limit"
                        product.mrp_verified_at = ""
                    products.append(product)
                    if len(products) % 100 == 0:
                        print(
                            f"[+] shard {shard + 1}: rows={len(products)} processed={idx} "
                            f"MRP checks={mrp_checks}/{mrp_verification_limit} MRP found={mrp_found}",
                            flush=True,
                        )
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
                "mrp_source_type": p.mrp_source_type,
                "mrp_verified_at": p.mrp_verified_at,
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
    parser.add_argument("--mrp-verification-limit", type=int, default=200)
    args = parser.parse_args()

    rows = scrape(args.target, args.pause, args.shard, args.shards, args.mrp_verification_limit)
    write_csv(rows, args.output)
    print(f"[+] ROWS: {len(rows)}", flush=True)
    print(f"[+] BRANDS: {len({p.brand for p in rows})}", flush=True)
    print(f"[+] CATEGORIES: {len({p.category for p in rows})}", flush=True)
    print(f"[+] RELEASE YEARS FILLED: {sum(p.release_year is not None for p in rows)}", flush=True)
    print(f"[+] MRP VALUES FOUND: {sum(p.mrp is not None for p in rows)}", flush=True)
    print(f"[+] MRP VALUES CHECKED: {sum(p.mrp_verified_at != '' for p in rows)}", flush=True)
    if len(rows) < args.target:
        print(f"ERROR: only {len(rows)} valid rows; target={args.target}; no synthetic rows added.",
              file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
