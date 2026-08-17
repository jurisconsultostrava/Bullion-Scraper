import os
import re
import json
import logging
from urllib.parse import urlparse, urljoin, parse_qsl
from flask import Flask, request, jsonify, render_template, Response
import requests
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup, Tag

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
    "gold": ["gold", "au", "oro"],
    "silver": ["silver", "ag", "argent"],
    "platinum": ["platinum", "pt", "platin"],
    "palladium": ["palladium", "pd"],
    "rhodium": ["rhodium", "rh"],
}

CURRENCY_SYMBOLS = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
    "Kč": "CZK",
    "CHF": "CHF",
    "AUD": "AUD",
    "CAD": "CAD",
    "HKD": "HKD",
    "SGD": "SGD",
    "NZD": "NZD",
}

# Short single-word menu labels to reject as product names
MENU_JUNK_WORDS = {
    "gold", "silver", "platinum", "palladium", "rhodium",
    "learn", "about", "contact", "shipping", "storage", "supplies",
    "new", "sale", "shop", "home", "more", "all", "buy",
    "stonex bullion", "stonex", "apmex", "european mint", "bullionbypost",
    "menu", "search", "cart", "account", "login", "register",
    "help", "faq", "guide", "blog", "news", "press",
}

PRODUCT_URL_PATTERNS = re.compile(
    r"/(gold|silver|platinum|palladium|rhodium|bullion|bar|coin|round|product|buy|shop|item|"
    r"gold-bar|silver-bar|gold-coin|silver-coin|platinum-bar|palladium-bar|"
    r"1-oz|5-oz|10-oz|1oz|kilo|ounce|troy|fine-weight|gram)/",
    re.IGNORECASE,
)

JUNK_URL_PATTERNS = re.compile(
    r"/(blog|news|article|guide|help|faq|about|contact|shipping|returns|policy|"
    r"login|register|account|cart|checkout|wishlist|search|category|tag|sitemap|"
    r"terms|privacy|legal|review|press|media|careers|partner|learn)/",
    re.IGNORECASE,
)

JUNK_URL_SUFFIXES = re.compile(
    r"\.(jpg|jpeg|png|gif|svg|pdf|zip|css|js|ico|xml|rss)$",
    re.IGNORECASE,
)

JUNK_TEXTS = re.compile(
    r"^(read more|learn more|click here|buy now|add to cart|view all|see all|"
    r"subscribe|sign up|log in|register|back to top|notify me|watchlist|"
    r"share|print|email|tweet|facebook|follow us|contact us|about us|"
    r"privacy policy|terms of service)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------

VENDOR_DOMAINS = {
    "stonex": ("stonexbullion.com",),
    "europeanmint": ("europeanmint.com",),
    "apmex": ("apmex.com",),
    "bullionbypost": ("bullionbypost.co.uk",),
    "zlatodomu": ("zlatodomu.cz",),
    "aurumpro": ("aurumpro.cz",),
    "goldenhouse": ("goldenhouse.cz",),
}

def _host_matches(host: str, domain: str) -> bool:
    """Exact or subdomain match only — never substring matching."""
    return host == domain or host.endswith("." + domain)

def detect_vendor(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    for vendor, domains in VENDOR_DOMAINS.items():
        if any(_host_matches(host, d) for d in domains):
            return vendor
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
        (r"(\d+(?:[.,]\d+)?)\s*(?:troy\s*)?oz(?:ounce)?(?!\w)", "oz"),
        (r"(\d+(?:[.,]\d+)?)\s*kilo(?:gram)?", "kg"),
        (r"(\d+(?:[.,]\d+)?)\s*kg(?!\w)", "kg"),
        (r"(\d+(?:[.,]\d+)?)\s*gram(?:s)?", "g"),
        (r"(\d+(?:[.,]\d+)?)\s*g(?!\w)", "g"),
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
    for symbol in ["Kč", "CHF", "AUD", "CAD", "HKD", "SGD", "NZD", "€", "$", "£"]:
        if symbol in text:
            return CURRENCY_SYMBOLS[symbol]
    text_upper = text.upper()
    for code in ["EUR", "USD", "GBP", "CZK", "CHF", "AUD", "CAD"]:
        if code in text_upper:
            return code
    return ""

def infer_price(text: str) -> float | None:
    if not text:
        return None
    # Zcela neprůstřelná metoda:
    # 1. Nejprve odstraníme veškeré myslitelné mezery (včetně pevných \xa0 apod.)
    clean_text = re.sub(r"[\s\xa0]", "", text)
    
    # 2. Vytáhneme pouze čistý blok s číslicemi, tečkami a čárkami
    m = re.search(r"([\d.,]+)", clean_text)
    if not m:
        return None
        
    raw = m.group(1).rstrip(",.")
    
    # 3. Určíme oddělovač desetinných míst
    comma_last = re.search(r",(\d{1,2})$", raw)
    dot_last = re.search(r"\.(\d{1,2})$", raw)
    
    if comma_last:
        raw = raw.replace(".", "").replace(",", ".")
    elif dot_last:
        raw = raw.replace(",", "")
    else:
        # Pokud číslo nemá desetinná místa, všechno ostatní jsou jen oddělovače tisíců (zahoď je)
        raw = raw.replace(",", "").replace(".", "")
        
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

def is_junk_name(name: str) -> bool:
    stripped = name.strip().lower()
    if stripped in MENU_JUNK_WORDS:
        return True
    if len(stripped) < 6:
        return True
    if JUNK_TEXTS.match(stripped):
        return True
    return False

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
                p = _normalize_jsonld_product(item)
                if p:
                    products.append(p)
            elif isinstance(item, dict) and item.get("@type") == "ListItem":
                inner = item.get("item", {})
                if isinstance(inner, dict) and inner.get("@type") == "Product":
                    p = _normalize_jsonld_product(inner)
                    if p:
                        products.append(p)
    return products

def _normalize_jsonld_product(item: dict) -> dict | None:
    name = item.get("name", "")
    if not name or is_junk_name(name):
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
    return {
        "_name": name,
        "_price": price,
        "_currency": currency,
        "_availability": availability,
        "_image": image,
        "_url": url,
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
        # Zjednodušený vzor – necháme těžkou práci na infer_price
        pattern = re.compile(
            rf"{metal}[^$€£\d]{{0,30}}([€$£]?\s*[\d\s\xa0]+(?:[,.]\d{{1,2}})?)",
            re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip()
            price = infer_price(raw)
            if price and price > 1:
                metals[metal] = {"price": price, "diff": None, "percent": None}
    return metals

# ---------------------------------------------------------------------------
# StoneX-specific parser
# ---------------------------------------------------------------------------

STONEX_BASE = "https://stonexbullion.com"

def parse_stonex(html: str, url: str) -> list[dict]:
    vendor = "StoneX Bullion"
    soup = BeautifulSoup(html, "lxml")
    products = []

    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))

    if products:
        return deduplicate(products)

    for a in soup.find_all("a", class_=lambda c: c and "product-item" in c):
        body = a.find(class_="product-thumb-body")
        if not body:
            continue
        desc_el = body.find(class_="product-thumb-description")
        if not desc_el:
            continue

        title_el = body.find(class_="card-title")
        name = title_el.get_text(strip=True) if title_el else ""
        if not name:
            img = a.find("img")
            name = img.get("alt", "") if img else ""
        name = " | ".join(p.strip() for p in name.split("|") if p.strip())
        if is_junk_name(name):
            continue

        desc_text = desc_el.get_text(strip=True)
        weight_g = infer_weight(desc_text)
        availability = ""
        m = re.search(r"Availability[:\s]*([\d,]+)", desc_text, re.IGNORECASE)
        cart_el = body.find(class_="cart-action-container")
        cart_text = cart_el.get_text(strip=True).lower() if cart_el else ""
        if "notify" in cart_text:
            availability = "Out of Stock"
        elif m:
            availability = f"In Stock ({m.group(1).replace(',', '')})"
        else:
            availability = "In Stock"

        price = None
        currency = "EUR"
        price_el = body.find(class_="product-thumb-price")
        if price_el is None:
            for child in body.children:
                if not isinstance(child, Tag):
                    continue
                if (child.get("class") or []) == ["py-3"]:
                    price_el = child
                    break
        if price_el is not None:
            price_text = price_el.get_text(strip=True)
            price = infer_price(price_text)
            currency = infer_currency(price_text) or "EUR"

        img = a.find("img")
        image_url = ""
        if img:
            image_url = img.get("src", "") or img.get("data-src", "")
        if image_url and not image_url.startswith("http"):
            image_url = STONEX_BASE + image_url

        href = a.get("href", "")
        if href and not href.startswith("http"):
            href = STONEX_BASE + href

        raw = {
            "_name": name,
            "_price": price,
            "_currency": currency,
            "_availability": availability,
            "_image": image_url,
            "_url": href,
            "_weight_g": weight_g,
        }
        products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)

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
        if not text or is_junk_name(text) or len(text) < 6:
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
            "_price": infer_price(text),
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
        name = name_tag.get_text(strip=True) if name_tag else ""
        if not name:
            name = a_tag.get_text(strip=True)
        if is_junk_name(name):
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
    weight_g = raw.get("_weight_g") or infer_weight(text_for_inference)
    product_number = str(raw.get("_product_number") or "").strip()
    price = raw.get("_price")
    currency = raw.get("_currency") or infer_currency(text_for_inference) or None
    availability = raw.get("_availability", "")
    image_url = raw.get("_image", "")
    if image_url and not image_url.startswith("http"):
        image_url = urljoin(source_url, image_url)
    image_url_2x = raw.get("_image_2x", "")
    if image_url_2x and not image_url_2x.startswith("http"):
        image_url_2x = urljoin(source_url, image_url_2x)
    category = raw.get("_category") or infer_category(url) or infer_metal(url)
    return {
        "vendor": vendor,
        "category": category,
        "name": name,
        "metal": metal,
        "weight_g": weight_g,
        "price": price,
        "currency": currency,
        "availability": availability,
        "product_number": product_number,
        "url": url,
        "image_url": image_url,
        "image_url_2x": image_url_2x,
        "images": [image_url] if image_url else [],
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
# ZlatoDomů.cz adapter
# ---------------------------------------------------------------------------

def parse_zlatodomu(html: str, url: str) -> list[dict]:
    vendor = "ZlatoDomů.cz"
    soup = BeautifulSoup(html, 'html.parser')
    products = []

    for card in soup.select('article.js-product'):
        try:
            title_element = card.select_one('h2.product-title a')
            name = title_element.text.strip() if title_element else None
            href = title_element['href'] if title_element else None
            if href and not href.startswith("http"):
                href = urljoin(url, href)

            price_element = card.select_one('span.price')
            price = None
            currency = "CZK"
            if price_element:
                price = infer_price(price_element.text)
                currency = infer_currency(price_element.text) or "CZK"

            product_number = card.get('data-id-product')
            
            img_tag = card.find("img")
            image_url = ""
            if img_tag:
                image_url = img_tag.get("src", "") or img_tag.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)
            
            avail_tag = card.select_one('.product-available')
            availability = avail_tag.text.strip() if avail_tag else ""

            if name and href:
                raw = {
                    '_name': name,
                    '_price': price,
                    '_currency': currency,
                    '_availability': availability,
                    '_image': image_url,
                    '_url': href,
                    '_product_number': product_number,
                }
                products.append(normalize_product(raw, vendor, url))
        except Exception as e:
            logger.warning(f"Error parsing ZlatoDomu item: {e}")

    # Fallback to structured data or generic parser if explicit CSS fails
    if not products:
        structured = parse_structured_data(html)
        for raw in structured:
            products.append(normalize_product(raw, vendor, url))
        if not products:
            cards = parse_generic_product_cards(soup, url)
            for raw in cards:
                products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)

# ---------------------------------------------------------------------------
# AurumPro.cz adapter
# ---------------------------------------------------------------------------

def parse_aurumpro(html: str, url: str) -> list[dict]:
    vendor = "AurumPro.cz"
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for card in soup.select('article.card-item'):
        try:
            title_element = card.select_one('h4.p-i-header a')
            name = title_element.text.strip() if title_element else None
            href = title_element['href'] if title_element else None
            if href and not href.startswith("http"):
                href = urljoin(url, href)

            price_element = card.select_one('.p-i-price strong')
            price = None
            currency = "CZK"
            if price_element:
                price = infer_price(price_element.text)
                currency = infer_currency(price_element.text) or "CZK"

            avail_element = card.select_one('.p-i-av')
            availability = avail_element.text.strip() if avail_element else ""

            product_number = card.get('data-product-id')

            img_tag = card.select_one('picture.p-i-img-1 img')
            image_url = ""
            if img_tag:
                image_url = img_tag.get("src", "") or img_tag.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)

            if name and href:
                raw = {
                    '_name': name,
                    '_price': price,
                    '_currency': currency,
                    '_availability': availability,
                    '_image': image_url,
                    '_url': href,
                    '_product_number': product_number,
                }
                products.append(normalize_product(raw, vendor, url))
        except Exception as e:
            logger.warning(f"Chyba při parsování AurumPro položky: {e}")

    if not products:
        structured = parse_structured_data(html)
        for raw in structured:
            products.append(normalize_product(raw, vendor, url))
        if not products:
            cards = parse_generic_product_cards(soup, url)
            for raw in cards:
                raw["_currency"] = raw.get("_currency") or "CZK"
                products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)

# ---------------------------------------------------------------------------
# GoldenHouse.cz adapter
# ---------------------------------------------------------------------------

def parse_goldenhouse(html: str, url: str) -> list[dict]:
    """Parse GoldenHouse category and product pages.

    GoldenHouse uses its own product codes, so these codes are useful as a
    competitor identifier rather than as a Shoptet pairing key.  Matching to
    our catalog should primarily use normalized name + metal + weight.
    """
    vendor = "GoldenHouse.cz"
    soup = BeautifulSoup(html, "lxml")
    products: list[dict] = []

    # 1) Prefer JSON-LD / structured product data where available.
    for raw in parse_structured_data(html):
        p = normalize_product(raw, vendor, url)
        if p.get("name") and p.get("price") is not None:
            products.append(p)

    # 2) Product detail page fallback.  Public pages expose labels such as
    #    "Kód:", "Hmotnost:" and the visible selling price.
    page_text = " ".join(soup.stripped_strings)
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""

    code_match = re.search(r"(?:K[oó]d|Code)\s*:\s*([A-Za-z0-9._/-]+)", page_text, re.IGNORECASE)
    weight_match = re.search(r"(?:Hmotnost|V[aá]ha|Weight)\s*:\s*(\d+(?:[.,]\d+)?)\s*(g|kg|oz)", page_text, re.IGNORECASE)

    # Selling price: take the first CZK amount after the title/code area and
    # explicitly avoid buyback / deferred-delivery labels.
    price = None
    currency = "CZK"
    price_candidates = []
    for m in re.finditer(r"(\d{1,3}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*K[cč]", page_text, re.IGNORECASE):
        left = page_text[max(0, m.start()-80):m.start()].lower()
        if any(x in left for x in ("výkup", "buyback", "odložen", "termínovan", "zvýhodněn")):
            continue
        val = infer_price(m.group(0))
        if val is not None:
            price_candidates.append(val)
    if price_candidates:
        price = price_candidates[0]

    availability = ""
    for pat in (
        r"Skladem",
        r"Dostupn(?:é|y)[^.;|]{0,40}",
        r"Available[^.;|]{0,40}",
        r"Doručení[^.;|]{0,40}",
    ):
        m = re.search(pat, page_text, re.IGNORECASE)
        if m:
            availability = m.group(0).strip()
            break

    img = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        img = urljoin(url, og.get("content"))
    if not img:
        it = soup.select_one('img[itemprop="image"], .product img, main img')
        if it:
            img = urljoin(url, it.get("src") or it.get("data-src") or "")

    if name and price is not None:
        raw = {
            "_name": name,
            "_price": price,
            "_currency": currency,
            "_availability": availability,
            "_image": img,
            "_url": url,
            "_product_number": code_match.group(1) if code_match else "",
        }
        if weight_match:
            val = float(weight_match.group(1).replace(",", "."))
            unit = weight_match.group(2).lower()
            raw["_weight_g"] = val * (1000 if unit == "kg" else 31.1034768 if unit == "oz" else 1)
        products.append(normalize_product(raw, vendor, url))

    # 3) Category/list fallback using anchors/cards around product links.
    if not products:
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            raw["_currency"] = raw.get("_currency") or "CZK"
            products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)

# ---------------------------------------------------------------------------
# European Mint adapter
# ---------------------------------------------------------------------------

def parse_european_mint(html: str, url: str) -> list[dict]:
    vendor = "European Mint"
    soup = BeautifulSoup(html, "lxml")
    products = []

    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))

    if not products:
        for item in soup.select(".product-item, .products .product, li.item, [class*='product-cell']"):
            name_el = item.find(["h2", "h3", "h4", "a"])
            name = name_el.get_text(strip=True) if name_el else ""
            if is_junk_name(name):
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
        cards = parse_generic_product_cards(soup, url)
        for raw in cards:
            products.append(normalize_product(raw, vendor, url))

    if not products:
        links = parse_generic_product_links(soup, url)
        for raw in links:
            products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)


# ---------------------------------------------------------------------------
# APMEX adapter
# ---------------------------------------------------------------------------

def parse_apmex(html: str, url: str) -> list[dict]:
    vendor = "APMEX"
    soup = BeautifulSoup(html, "lxml")
    products = []

    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))

    if not products:
        seen_hrefs = set()
        for a_el in soup.select("a[href^='/product/']"):
            href = a_el.get("href", "")
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            name = a_el.get_text(strip=True)
            if not name or is_junk_name(name):
                img_in = a_el.find("img")
                name = (img_in.get("alt", "").strip() if img_in else "") or ""
            if not name or is_junk_name(name):
                continue
            container = a_el.find_parent(["div", "li", "article"]) or a_el
            price_el = container.find(class_=re.compile(r"price", re.IGNORECASE))
            price = infer_price(price_el.get_text()) if price_el else None
            img = container.find("img") or a_el.find("img")
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
                "_url": urljoin(url, href),
            }
            products.append(normalize_product(raw, vendor, url))

    return deduplicate(products)


# ---------------------------------------------------------------------------
# BullionByPost adapter
# ---------------------------------------------------------------------------

def parse_bullionbypost(html: str, url: str) -> list[dict]:
    vendor = "BullionByPost"
    soup = BeautifulSoup(html, "lxml")
    products = []

    structured = parse_structured_data(html)
    for raw in structured:
        products.append(normalize_product(raw, vendor, url))

    if not products:
        for card in soup.select("div.card.category-module, div.card.product-module"):
            name_el = card.select_one("p.product-name a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name or is_junk_name(name):
                continue
            href = urljoin(url, name_el.get("href", ""))
            price_el = card.select_one("span.price")
            price = infer_price(price_el.get_text()) if price_el else None
            stock_el = card.select_one(".stock-message")
            availability = stock_el.get_text(strip=True) if stock_el else ""
            pid_el = card.select_one("[data-price-product-id]")
            product_number = pid_el.get("data-price-product-id", "") if pid_el else ""
            img = card.select_one(".product-image img") or card.find("img")
            image_url = ""
            if img:
                image_url = img.get("src", "") or img.get("data-src", "")
                if image_url:
                    image_url = urljoin(url, image_url)
            raw = {
                "_name": name,
                "_price": price,
                "_currency": "GBP",
                "_availability": availability,
                "_image": image_url,
                "_url": href,
                "_product_number": product_number,
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


# ---------------------------------------------------------------------------
# Generic adapter
# ---------------------------------------------------------------------------

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
    "zlatodomu": parse_zlatodomu,
    "aurumpro": parse_aurumpro,
    "goldenhouse": parse_goldenhouse,
    "generic": parse_generic,
}

VENDOR_DISPLAY_NAMES = {
    "stonex": "StoneX Bullion",
    "europeanmint": "European Mint",
    "apmex": "APMEX",
    "bullionbypost": "BullionByPost",
    "zlatodomu": "ZlatoDomů.cz",
    "aurumpro": "AurumPro.cz",
    "goldenhouse": "GoldenHouse.cz",
    "generic": "Generic",
}

class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

IMPERSONATE_PROFILES = ["firefox135", "safari18_0", "chrome131"]

CF_CHALLENGE_MARKERS = ("just a moment", "challenges.cloudflare.com")

def fetch_html(url: str) -> tuple[str, str]:
    last_error: str = "unknown error"
    last_status: int | None = None
    for profile in IMPERSONATE_PROFILES:
        try:
            resp = cffi_requests.get(
                url,
                impersonate=profile,
                timeout=FETCH_TIMEOUT,
                allow_redirects=True,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        body_head = resp.text[:2000].lower()
        is_challenge = any(m in body_head for m in CF_CHALLENGE_MARKERS)
        if resp.status_code == 200 and not is_challenge:
            return resp.text, str(resp.url) or url
        last_status = resp.status_code
        if is_challenge:
            last_error = (
                f"blocked by Cloudflare bot challenge (HTTP {resp.status_code}, "
                f"profile {profile}). This site requires JavaScript execution "
                "and cannot be fetched server-side."
            )
        else:
            last_error = f"upstream returned HTTP {resp.status_code} (profile {profile})"
    raise FetchError(last_error, status=last_status)

MAX_PAGES = 5

def find_pagination_urls(html: str, url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    base = urlparse(url)
    page_numbers = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"[?&]page=(\d+)", href)
        if not m:
            continue
        full = urljoin(url, href)
        pu = urlparse(full)
        if pu.netloc == base.netloc and pu.path == base.path:
            page_numbers.add(int(m.group(1)))
    if not page_numbers:
        return []
    last_page = min(max(page_numbers), MAX_PAGES)
    clean = url.split("#")[0]
    sep = "&" if "?" in clean else "?"
    return [f"{clean}{sep}page={n}" for n in range(2, last_page + 1)]

# ---------------------------------------------------------------------------
# Product-number enrichment (StoneX)
# ---------------------------------------------------------------------------

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_product_number_cache: dict[str, str] = {}
_product_number_cache_lock = threading.Lock()
_SKU_RE = re.compile(r'"sku"\s*:\s*"([^"]+)"')
_PN_ROW_RE = re.compile(
    r"Product number</td>\s*<td[^>]*>([^<]+)</td>", re.IGNORECASE
)
ENRICH_MAX_WORKERS = 6
ENRICH_TIMEOUT = 15

def _is_stonex_url(product_url: str) -> bool:
    try:
        host = urlparse(product_url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(_host_matches(host, d) for d in VENDOR_DOMAINS["stonex"])

def _fetch_stonex_product_number(product_url: str) -> str:
    with _product_number_cache_lock:
        if product_url in _product_number_cache:
            return _product_number_cache[product_url]
    try:
        resp = cffi_requests.get(
            product_url, impersonate="firefox135", timeout=ENRICH_TIMEOUT
        )
        if resp.status_code != 200:
            return ""
        m = _SKU_RE.search(resp.text) or _PN_ROW_RE.search(resp.text)
        number = m.group(1).strip() if m else ""
        if number:
            with _product_number_cache_lock:
                _product_number_cache[product_url] = number
        return number
    except Exception:
        logger.warning("Product-number fetch failed for %s", product_url)
        return ""

def enrich_stonex_product_numbers(products: list[dict]) -> None:
    targets = [
        p for p in products
        if not p.get("product_number") and _is_stonex_url(p.get("url", ""))
    ]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_stonex_product_number, p["url"]): p for p in targets
        }
        for fut in as_completed(futures):
            futures[fut]["product_number"] = fut.result()

_gallery_cache: dict[str, list] = {}
_gallery_cache_lock = threading.Lock()

def _fetch_stonex_gallery(product_url: str) -> list[str]:
    path = urlparse(product_url).path
    with _gallery_cache_lock:
        if path in _gallery_cache:
            return _gallery_cache[path]
    try:
        resp = cffi_requests.post(
            STONEX_ROUTER_API_URL,
            impersonate="firefox135",
            timeout=ENRICH_TIMEOUT,
            json={"path": path},
        )
        if resp.status_code != 200:
            return []
        data = resp.json().get("data") or {}
        images = (data.get("product") or {}).get("images") or {}
        urls: list[str] = []
        for snippet in images.get("list") or []:
            u = _parse_srcset(snippet).get("1x", "")
            if u and u not in urls:
                urls.append(u)
        if urls:
            with _gallery_cache_lock:
                _gallery_cache[path] = urls
        return urls
    except Exception:
        logger.warning("Gallery fetch failed for %s", product_url)
        return []

def enrich_stonex_galleries(products: list[dict]) -> None:
    targets = [p for p in products if _is_stonex_url(p.get("url", ""))]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_stonex_gallery, p["url"]): p for p in targets
        }
        for fut in as_completed(futures):
            p = futures[fut]
            gallery = fut.result()
            if gallery:
                p["images"] = gallery

def enrich_stonex(products: list[dict]) -> None:
    enrich_stonex_product_numbers(products)
    enrich_stonex_galleries(products)

ENRICHERS = {
    "stonex": enrich_stonex,
}

# ---------------------------------------------------------------------------
# StoneX catalog JSON API
# ---------------------------------------------------------------------------

STONEX_API_URL = "https://stonexbullion.com/api/client/catalog/"
STONEX_ROUTER_API_URL = "https://stonexbullion.com/api/client/router/"
STONEX_CURRENCIES = {"EUR", "USD", "GBP", "PLN", "CHF", "SGD", "AUD", "CZK", "CAD"}
STONEX_API_MAX_PAGES = 10
_TAG_RE = re.compile(r"<[^>]+>")

STONEX_AVAILABILITY_LABELS = {
    "in_stock": "In Stock",
    "out_of_stock": "Out of Stock",
    "pre_sale": "Pre-Sale",
}

def _parse_bracket_params(query: str) -> dict:
    params: dict = {}
    for key, value in parse_qsl(query, keep_blank_values=False):
        base = key.split("[", 1)[0]
        if value.isdigit():
            value = int(value)
        if "[" in key:
            params.setdefault(base, []).append(value)
        else:
            params[base] = value
    params.pop("page", None)
    return params

def _stonex_api_page(body: dict, currency: str) -> dict:
    resp = cffi_requests.post(
        STONEX_API_URL,
        impersonate="firefox135",
        timeout=FETCH_TIMEOUT,
        json=body,
        headers={"Accept": "application/json", "X-Currency": currency},
    )
    if resp.status_code != 200:
        raise FetchError(
            f"StoneX API returned HTTP {resp.status_code}", status=resp.status_code
        )
    data = json.loads(resp.text)
    if not data.get("success"):
        raise FetchError(f"StoneX API error: {data.get('message') or data.get('code')}")
    return data["data"]["catalog"]

_SRCSET_RE = re.compile(r'srcset="([^"]+)"')

def _parse_srcset(picture_html: str) -> dict[str, str]:
    m = _SRCSET_RE.search(picture_html or "")
    if not m:
        return {}
    out = {}
    for entry in m.group(1).split(","):
        parts = entry.strip().split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
        elif len(parts) == 1 and parts[0]:
            out.setdefault("1x", parts[0])
    return out

def _stonex_api_product_to_raw(p: dict) -> dict:
    avail = p.get("availability") or {}
    code = avail.get("code", "")
    availability = STONEX_AVAILABILITY_LABELS.get(
        code, _TAG_RE.sub(" ", avail.get("short_text", "") or code).strip()
    )
    images = p.get("images") or {}
    image = ""
    image_2x = ""
    if isinstance(images, dict):
        srcset = _parse_srcset(images.get("main", ""))
        image = srcset.get("1x", "") or images.get("small", "")
        image_2x = srcset.get("2x", "")
    return {
        "_name": p.get("name", ""),
        "_price": p.get("gross_price"),
        "_availability": availability,
        "_image": image,
        "_image_2x": image_2x,
        "_url": p.get("url", ""),
        "_product_number": p.get("part_number", ""),
    }

def scrape_stonex_api(url: str, currency: str) -> tuple[list[dict], int]:
    parsed = urlparse(url)
    body = _parse_bracket_params(parsed.query)
    body["url"] = parsed.path
    body["page"] = 1
    catalog = _stonex_api_page(body, currency)
    paginator = catalog.get("paginator") or {}
    last_page = min(int(paginator.get("last_page") or 1), STONEX_API_MAX_PAGES)
    raw_products = list(catalog.get("products") or [])
    pages_fetched = 1
    for page in range(2, last_page + 1):
        body["page"] = page
        page_catalog = _stonex_api_page(body, currency)
        raw_products.extend(page_catalog.get("products") or [])
        pages_fetched += 1
    products = []
    for p in raw_products:
        raw = _stonex_api_product_to_raw(p)
        raw["_currency"] = currency
        products.append(normalize_product(raw, "stonex", url))
    return deduplicate(products), pages_fetched

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
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
        html, _ = fetch_html(url)
        return Response(html, status=200, mimetype="text/plain")
    except FetchError as e:
        return Response(f"Fetch error: {e}", status=502, mimetype="text/plain")

@app.route("/scrape")
def scrape_route():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid or missing URL"}), 400
    currency = request.args.get("currency", "EUR").strip().upper() or "EUR"
    if currency not in STONEX_CURRENCIES:
        return jsonify({
            "error": f"Unsupported currency '{currency}'. "
                     f"Supported: {', '.join(sorted(STONEX_CURRENCIES))}"
        }), 400

    if detect_vendor(url) == "stonex":
        try:
            products, pages_fetched = scrape_stonex_api(url, currency)
        except Exception as e:
            logger.warning("StoneX API scrape failed for %s: %s", url, e)
            products, pages_fetched = [], 0
        if products:
            try:
                enrich_stonex_galleries(products)
            except Exception:
                logger.exception("Gallery enrichment failed for %s", url)
            return jsonify({
                "source": url,
                "vendor": VENDOR_DISPLAY_NAMES.get("stonex", "stonex"),
                "title": "StoneX Bullion (catalog API)",
                "html_length": 0,
                "metals": sorted({p["metal"] for p in products if p.get("metal")}),
                "currency": currency,
                "pages_fetched": pages_fetched,
                "products": products,
            })

    try:
        html, final_url = fetch_html(url)
    except FetchError as e:
        return jsonify({"error": f"Fetch failed: {e}", "upstream_status": e.status}), 502

    url = final_url
    vendor_key = detect_vendor(url)
    adapter = VENDOR_ADAPTERS.get(vendor_key, parse_generic)
    try:
        products = adapter(html, url)
    except Exception as e:
        logger.exception("Parser error for %s", url)
        return jsonify({"error": f"Parse error: {e}"}), 500

    pages_fetched = 1
    if "page=" not in url:
        for page_url in find_pagination_urls(html, url):
            try:
                page_html, _ = fetch_html(page_url)
            except FetchError as e:
                logger.warning("Pagination fetch failed for %s: %s", page_url, e)
                break
            try:
                products.extend(adapter(page_html, page_url))
            except Exception:
                logger.exception("Parser error for paginated %s", page_url)
                break
            pages_fetched += 1
    products = deduplicate(products)

    enricher = ENRICHERS.get(vendor_key)
    if enricher:
        try:
            enricher(products)
        except Exception:
            logger.exception("Enrichment failed for %s", url)

    soup = BeautifulSoup(html, "lxml")
    title = (
        soup.title.string.strip()
        if soup.title and soup.title.string
        else url
    )
    metals = extract_metals(html)
    return jsonify({
        "source": url,
        "vendor": VENDOR_DISPLAY_NAMES.get(vendor_key, vendor_key),
        "title": title,
        "html_length": len(html),
        "metals": metals,
        "pages_fetched": pages_fetched,
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
    title = (
        soup.title.string.strip()
        if soup.title and soup.title.string
        else source_url
    )
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
