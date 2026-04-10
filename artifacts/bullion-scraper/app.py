import os
import re
import json
import logging
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify, render_template, Response
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

FETCH_TIMEOUT = 20

OZ_TO_G = 31.1034768

METAL_KEYWORDS = {
    "gold": ["gold", "au", "oro", "gold bar", "gold coin"],
    "silver": ["silver", "ag", "argent", "silver bar", "silver coin"],
    "platinum": ["platinum", "pt", "platin"],
    "palladium": ["palladium", "pd"],
    "rhodium": ["rhodium", "rh"],
}

CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "Kč": "CZK",
    "CHF": "CHF",
    "AUD": "AUD",
    "CAD": "CAD",
    "HKD": "HKD",
    "SGD": "SGD",
    "NZD": "NZD",
}

PRODUCT_URL_PATTERNS = re.compile(
    r"/(gold|silver|platinum|palladium|rhodium|bullion|bar|coin|round|product|buy|shop|item|"
    r"gold-bar|silver-bar|gold-coin|silver-coin|platinum-bar|palladium-bar|"
    r"1-oz|5-oz|10-oz|1oz|kilo|ounce|troy)/",
    re.IGNORECASE,
)

JUNK_URL_PATTERNS = re.compile(
    r"/(blog|news|article|guide|help|faq|about|contact|shipping|returns|policy|"
    r"login|register|account|cart|checkout|wishlist|search|category|tag|sitemap|"
    r"terms|privacy|legal|review|press|media|careers|partner)/",
    re.IGNORECASE,
)

JUNK_URL_SUFFIXES = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|pdf|zip|css|js|ico|xml|rss)$",
    re.IGNORECASE,
)

JUNK_TEXTS = re.compile(
    r"(read more|learn more|click here|buy now|add to cart|view all|see all|"
    r"subscribe|sign up|log in|register|back to top|share|print|email|tweet|facebook|"
    r"follow us|contact us|about us|privacy policy|terms of service)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------

def detect_vendor(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = host.replace("www.", "")
    if "stonex" in host or "stonexbullion" in host:
        return "stonex"
    if "europeanmint" in host or "european-mint" in host:
        return "europeanmint"
    if "apmex" in host:
        return "apmex"
    if "bullionbypost" in host:
        return "bullionbypost"
    return "generic"


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------

def infer_metal(text: str) -> str:
    text_lower = text.lower()
    for metal, keywords in METAL_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return metal
    return ""


def infer_weight(text: str) -> float | None:
    text_lower = text.lower()
    patterns = [
        (r"(\d+(?:[.,]\d+)?)\s*(?:troy\s*)?oz(?:ounce)?", "oz"),
        (r"(\d+(?:[.,]\d+)?)\s*kg", "kg"),
        (r"(\d+(?:[.,]\d+)?)\s*g(?:ram)?(?!\w)", "g"),
        (r"(\d+(?:[.,]\d+)?)\s*gram", "g"),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, text_lower)
        if m:
            val = float(m.group(1).replace(",", "."))
            if unit == "oz":
                return round(val * OZ_TO_G, 4)
            elif unit == "kg":
                return round(val * 1000, 4)
            else:
                return val
    return None


def infer_currency(text: str) -> str:
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    text_upper = text.upper()
    for code in ["USD", "EUR", "GBP", "CZK", "CHF", "AUD", "CAD"]:
        if code in text_upper:
            return code
    return ""


def infer_price(text: str) -> float | None:
    m = re.search(r"[\$€£]?\s?(\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{1,2})?)", text)
    if m:
        raw = m.group(1).replace(",", "").replace(" ", "")
        try:
            val = float(raw)
            if val > 0.5:
                return val
        except ValueError:
            pass
    return None


def infer_category(url: str) -> str:
    path = urlparse(url).path.lower()
    if "gold" in path:
        return "gold"
    if "silver" in path:
        return "silver"
    if "platinum" in path:
        return "platinum"
    if "palladium" in path:
        return "palladium"
    return ""


# ---------------------------------------------------------------------------
# Structured data extraction (JSON-LD / schema.org)
# ---------------------------------------------------------------------------

def parse_structured_data(html: str) -> list[dict]:
    products = []
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif data.get("@type") == "Product":
                items = [data]
            elif "@graph" in data:
                items = data["@graph"]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("Product", "product"):
                products.append(_normalize_jsonld_product(item))
            elif isinstance(item, dict) and item.get("@type") == "ListItem":
                inner = item.get("item", {})
                if isinstance(inner, dict) and inner.get("@type") == "Product":
                    products.append(_normalize_jsonld_product(inner))
    return [p for p in products if p]


def _normalize_jsonld_product(item: dict) -> dict | None:
    name = item.get("name", "")
    if not name:
        return None
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price_raw = str(offers.get("price", ""))
    price = None
    if price_raw:
        try:
            price = float(price_raw.replace(",", ""))
        except ValueError:
            pass
    currency = offers.get("priceCurrency", "") or infer_currency(price_raw)
    availability_raw = offers.get("availability", "")
    availability = availability_raw.split("/")[-1] if availability_raw else ""
    image = item.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    url = item.get("url", "") or offers.get("url", "")
    sku = item.get("sku", "")
    return {
        "_name": name,
        "_price": price,
        "_currency": currency,
        "_availability": availability,
        "_image": image,
        "_url": url,
        "_sku": sku,
    }


# ---------------------------------------------------------------------------
# Metals spot price extraction
# ---------------------------------------------------------------------------

def extract_metals(html: str) -> dict:
    metals = {}
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ")
    metal_names = ["gold", "silver", "platinum", "palladium", "rhodium"]
    for metal in metal_names:
        pattern = re.compile(
            rf"{metal}[^$€£\d]*([€$£]?\s?\d{{1,3}}(?:[,.]\d{{3}})*(?:[,.]\d{{1,2}})?)",
            re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip()
            price = infer_price(raw)
            if price:
                metals[metal] = {"price": price, "diff": None, "percent": None}
    return metals


# ---------------------------------------------------------------------------
# Generic link / card parsers
# ---------------------------------------------------------------------------

def _is_product_url(url: str, base_url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if JUNK_URL_SUFFIXES.search(url):
        return False
    if JUNK_URL_PATTERNS.search(url):
        return False
    parsed = urlparse(url)
    base_parsed = urlparse(base_url)
    if parsed.netloc and parsed.netloc != base_parsed.netloc:
        return False
    return bool(PRODUCT_URL_PATTERNS.search(url))


def parse_generic_product_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    seen = set()
    results = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href in seen:
            continue
        if not _is_product_url(href, base_url):
            continue
        text = a.get_text(separator=" ", strip=True)
        if not text or JUNK_TEXTS.search(text) or len(text) < 5:
            continue
        if len(text) > 200:
            continue
        img_tag = a.find("img")
        image_url = ""
        if img_tag:
            image_url = img_tag.get("src", "") or img_tag.get("data-src", "")
            if image_url:
                image_url = urljoin(base_url, image_url)
        seen.add(href)
        results.append({
            "_name": text,
            "_price": None,
            "_currency": infer_currency(text),
            "_availability": "",
            "_image": image_url,
            "_url": href,
        })
    return results


def parse_generic_product_cards(soup: BeautifulSoup, base_url: str) -> list[dict]:
    seen = set()
    results = []
    card_selectors = [
        "[class*='product']",
        "[class*='item']",
        "[class*='card']",
        "li.product",
        "article",
    ]
    cards = []
    for sel in card_selectors:
        try:
            found = soup.select(sel)
            if found:
                cards.extend(found)
        except Exception:
            pass
    for card in cards:
        a_tag = card.find("a", href=True)
        if not a_tag:
            continue
        href = urljoin(base_url, a_tag["href"])
        if href in seen:
            continue
        if JUNK_URL_PATTERNS.search(href):
            continue
        if JUNK_URL_SUFFIXES.search(href):
            continue
        name_tag = (
            card.find(["h2", "h3", "h4", "h1"])
            or card.find("[class*='title']")
            or card.find("[class*='name']")
        )
        name = ""
        if name_tag:
            name = name_tag.get_text(strip=True)
        if not name:
            name = a_tag.get_text(strip=True)
        if not name or len(name) < 5 or JUNK_TEXTS.search(name):
            continue
        price_tag = card.find(
            class_=re.compile(r"price|cost|amount", re.IGNORECASE)
        ) or card.find(["strong", "b"], string=re.compile(r"[\d.,]+"))
        price = None
        currency = ""
        if price_tag:
            price_text = price_tag.get_text(strip=True)
            price = infer_price(price_text)
            currency = infer_currency(price_text)
        img_tag = card.find("img")
        image_url = ""
        if img_tag:
            image_url = (
                img_tag.get("src", "")
                or img_tag.get("data-src", "")
                or img_tag.get("data-lazy-src", "")
            )
            if image_url:
                image_url = urljoin(base_url, image_url)
        avail_tag = card.find(class_=re.compile(r"stock|avail|availability", re.IGNORECASE))
        availability = avail_tag.get_text(strip=True) if avail_tag else ""
        seen.add(href)
        results.append({
            "_name": name,
            "_price": price,
            "_currency": currency,
            "_availability": availability,
            "_image": image_url,
            "_url": href,
        })
    return results


# ---------------------------------------------------------------------------
# Normalize raw product dict → final product schema
# ---------------------------------------------------------------------------

def normalize_product(raw: dict, vendor: str, source_url: str) -> dict:
    name = raw.get("_name", "").strip()
    url = raw.get("_url", "").strip() or source_url
    if not url.startswith("http"):
        url = urljoin(source_url, url)
    text_for_inference = f"{name} {url}"
    metal = infer_metal(text_for_inference)
    weight_g = infer_weight(text_for_inference)
    price = raw.get("_price")
    currency = raw.get("_currency") or infer_currency(text_for_inference)
    availability = raw.get("_availability", "")
    image_url = raw.get("_image", "")
    if image_url and not image_url.startswith("http"):
        image_url = urljoin(source_url, image_url)
    category = raw.get("_category") or infer_category(url) or infer_metal(url)
    return {
        "vendor": vendor,
        "category": category,
        "name": name,
        "metal": metal,
        "weight_g": weight_g,
        "price": price,
        "currency": currency or None,
        "availability": availability,
        "url": url,
        "image_url": image_url,
    }


def deduplicate(products: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for p in products:
        key = p.get("url", "").rstrip("/").split("?")[0]
        if key and key not in seen:
            seen.add(key)
            result.append(p)
    return result


# ---------------------------------------------------------------------------
# Vendor adapters
# ---------------------------------------------------------------------------

def parse_stonex(html: str, url: str) -> list[dict]:
    vendor = "StoneX Bullion"
    soup = BeautifulSoup(html, "lxml")
    products = []
    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))
    if not products:
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            products.append(normalize_product(raw, vendor, url))
    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            products.append(normalize_product(raw, vendor, url))
    return deduplicate(products)


def parse_european_mint(html: str, url: str) -> list[dict]:
    vendor = "European Mint"
    soup = BeautifulSoup(html, "lxml")
    products = []
    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))
    if not products:
        for item in soup.select(".product-item, .products .product, li.item"):
            name_el = item.find(["h2", "h3", "h4", "a"])
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            a_el = item.find("a", href=True)
            href = urljoin(url, a_el["href"]) if a_el else url
            price_el = item.find(class_=re.compile(r"price", re.IGNORECASE))
            price = infer_price(price_el.get_text()) if price_el else None
            currency = infer_currency(price_el.get_text()) if price_el else "EUR"
            img = item.find("img")
            image_url = ""
            if img:
                image_url = img.get("src", "") or img.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)
            raw = {
                "_name": name,
                "_price": price,
                "_currency": currency or "EUR",
                "_availability": "",
                "_image": image_url,
                "_url": href,
            }
            products.append(normalize_product(raw, vendor, url))
    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            products.append(normalize_product(raw, vendor, url))
    return deduplicate(products)


def parse_apmex(html: str, url: str) -> list[dict]:
    vendor = "APMEX"
    soup = BeautifulSoup(html, "lxml")
    products = []
    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))
    if not products:
        for item in soup.select(".product-title, .item-description, [class*='productItem'], [data-product-id]"):
            name_el = item.find(["h2", "h3", "h4", "span", "a"])
            name = name_el.get_text(strip=True) if name_el else item.get_text(strip=True)
            if not name or len(name) < 5:
                continue
            a_el = item.find("a", href=True) or item.find_parent("a")
            href = urljoin(url, a_el["href"]) if a_el else url
            price_el = item.find(class_=re.compile(r"price", re.IGNORECASE))
            price = infer_price(price_el.get_text()) if price_el else None
            img = item.find("img")
            image_url = ""
            if img:
                image_url = img.get("src", "") or img.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)
            raw = {
                "_name": name,
                "_price": price,
                "_currency": "USD",
                "_availability": "",
                "_image": image_url,
                "_url": href,
            }
            products.append(normalize_product(raw, vendor, url))
    if not products:
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            raw["_currency"] = raw.get("_currency") or "USD"
            products.append(normalize_product(raw, vendor, url))
    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            raw["_currency"] = raw.get("_currency") or "USD"
            products.append(normalize_product(raw, vendor, url))
    return deduplicate(products)


def parse_bullionbypost(html: str, url: str) -> list[dict]:
    vendor = "BullionByPost"
    soup = BeautifulSoup(html, "lxml")
    products = []
    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))
    if not products:
        for item in soup.select(".product-cell, .listing-item, .product-listing li, [class*='productCell']"):
            name_el = item.find(["h2", "h3", "h4", "a"])
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            a_el = item.find("a", href=True)
            href = urljoin(url, a_el["href"]) if a_el else url
            price_el = item.find(class_=re.compile(r"price|cost", re.IGNORECASE))
            price = infer_price(price_el.get_text()) if price_el else None
            img = item.find("img")
            image_url = ""
            if img:
                image_url = img.get("src", "") or img.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)
            raw = {
                "_name": name,
                "_price": price,
                "_currency": "GBP",
                "_availability": "",
                "_image": image_url,
                "_url": href,
            }
            products.append(normalize_product(raw, vendor, url))
    if not products:
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            raw["_currency"] = raw.get("_currency") or "GBP"
            products.append(normalize_product(raw, vendor, url))
    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            raw["_currency"] = raw.get("_currency") or "GBP"
            products.append(normalize_product(raw, vendor, url))
    return deduplicate(products)


def parse_generic(html: str, url: str) -> list[dict]:
    vendor_domain = urlparse(url).netloc.replace("www.", "")
    vendor = vendor_domain.split(".")[0].capitalize()
    soup = BeautifulSoup(html, "lxml")
    products = []
    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))
    if not products:
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            products.append(normalize_product(raw, vendor, url))
    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            products.append(normalize_product(raw, vendor, url))
    return deduplicate(products)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

VENDOR_ADAPTERS = {
    "stonex": parse_stonex,
    "europeanmint": parse_european_mint,
    "apmex": parse_apmex,
    "bullionbypost": parse_bullionbypost,
    "generic": parse_generic,
}

VENDOR_DISPLAY_NAMES = {
    "stonex": "StoneX Bullion",
    "europeanmint": "European Mint",
    "apmex": "APMEX",
    "bullionbypost": "BullionByPost",
    "generic": "Generic",
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "bullion-multi-scraper", "version": "v1"})


@app.route("/fetch")
def fetch_route():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return Response("Invalid or missing URL", status=400, mimetype="text/plain")
    try:
        html = fetch_html(url)
        return Response(html, status=200, mimetype="text/plain")
    except requests.exceptions.RequestException as e:
        return Response(f"Fetch error: {e}", status=502, mimetype="text/plain")


@app.route("/scrape")
def scrape_route():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid or missing URL"}), 400
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 502
    vendor_key = detect_vendor(url)
    adapter = VENDOR_ADAPTERS.get(vendor_key, parse_generic)
    try:
        products = adapter(html, url)
    except Exception as e:
        logger.exception("Parser error for %s", url)
        return jsonify({"error": f"Parse error: {e}"}), 500
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    metals = extract_metals(html)
    return jsonify({
        "source": url,
        "vendor": VENDOR_DISPLAY_NAMES.get(vendor_key, vendor_key),
        "title": title,
        "html_length": len(html),
        "metals": metals,
        "products": products,
    })


@app.route("/parse", methods=["POST"])
def parse_route():
    html = request.get_data(as_text=True)
    if not html:
        return jsonify({"error": "No HTML body provided"}), 400
    vendor_param = request.args.get("vendor", "generic").strip().lower()
    source_url = request.args.get("url", "https://example.com").strip()
    adapter = VENDOR_ADAPTERS.get(vendor_param, parse_generic)
    try:
        products = adapter(html, source_url)
    except Exception as e:
        logger.exception("Parse error")
        return jsonify({"error": f"Parse error: {e}"}), 500
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else source_url
    metals = extract_metals(html)
    return jsonify({
        "source": source_url,
        "vendor": VENDOR_DISPLAY_NAMES.get(vendor_param, vendor_param),
        "title": title,
        "html_length": len(html),
        "metals": metals,
        "products": products,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
