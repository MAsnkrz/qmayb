"""
Qogita Maybelline Monitor
Monitors https://www.qogita.com/brands/maybelline/

Tracks per product:
  - Lowest unit price across all suppliers
  - Total stock available
  - New product listings
  - Price drops / increases
  - Restocks / stock drops
  - Out of stock / back in stock

Requires: Qogita account (prices/stock only visible when logged in)
Deps: pip install playwright requests beautifulsoup4
      python -m playwright install chromium --with-deps
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BRAND_URL      = "https://www.qogita.com/brands/maybelline/"
BASE_URL       = "https://www.qogita.com"
SNAPSHOT_FILE  = "snapshot_qogita.json"
REQUEST_DELAY  = 3.0
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
HEADLESS       = os.getenv("HEADLESS", "true").lower() == "true"

# Qogita login credentials
QOGITA_EMAIL    = os.getenv("QOGITA_EMAIL",    "dapaplays@gmail.com")
QOGITA_PASSWORD = os.getenv("QOGITA_PASSWORD", "Sufsat-gucqum-5detse")

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1516449009055825992/bR6nty2UhaUJ67kBv3OZaifmQFg665oApOIj-Wnj-TjrFzMTEkKIfkEk0Yhex1PsdQC1")

# Discord colours
COLOUR_NEW        = 0xE91E8C
COLOUR_PRICE_DROP = 0x2ECC71
COLOUR_PRICE_UP   = 0xE74C3C
COLOUR_RESTOCK    = 0x3498DB
COLOUR_LOW_STOCK  = 0xF39C12
COLOUR_OOS        = 0x95A5A6
COLOUR_BACK       = 0x9B59B6

# ---------------------------------------------------------------------------
# BROWSER
# ---------------------------------------------------------------------------

def make_browser(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        viewport={"width": 1280, "height": 900},
    )
    return browser, context


def login(context):
    """Log in to Qogita. Returns True on success."""
    print("  Logging in to Qogita...")
    page = context.new_page()
    try:
        page.goto(f"{BASE_URL}/login/", timeout=30000, wait_until="networkidle")
        time.sleep(2)

        # Dismiss cookie consent banner if present
        for selector in ['button:has-text("Accept")', 'button:has-text("Accept all")',
                         'button:has-text("Allow all")', '[id*="cookie"] button',
                         '[class*="cookie"] button', '[class*="consent"] button']:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    break
            except Exception:
                pass

        # Wait for email field
        page.wait_for_selector('input[type="email"]', timeout=10000)
        time.sleep(0.5)

        # Clear and fill fields
        page.click('input[type="email"]')
        page.fill('input[type="email"]', QOGITA_EMAIL)
        time.sleep(0.3)
        page.click('input[type="password"]')
        page.fill('input[type="password"]', QOGITA_PASSWORD)
        time.sleep(0.3)

        # Submit
        page.click('button[type="submit"]')
        print(f"  Submitted login form, waiting for redirect...")

        # Wait for navigation away from login page
        try:
            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        except PWTimeout:
            # Check current URL anyway
            pass

        time.sleep(2)
        current_url = page.url
        print(f"  Post-login URL: {current_url}")

        if "/login" in current_url:
            # Try to get error message
            error_el = page.query_selector('[class*="error"], [class*="alert"], [role="alert"]')
            error_msg = error_el.inner_text() if error_el else "unknown"
            print(f"  [!] Login failed — still on login page. Error: {error_msg}")
            return False

        print(f"  Logged in successfully")
        return True
    except Exception as e:
        print(f"  [!] Login error: {e}")
        return False
    finally:
        page.close()


def fetch_page_html(context, url, wait_selector=None, timeout=25000):
    """Fetch a page and return HTML content."""
    page = context.new_page()
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000)
            except PWTimeout:
                pass
        # Extra wait for JS rendering
        time.sleep(2)
        return page.content()
    except Exception as e:
        print(f"  [!] Fetch error ({url}): {e}")
        return None
    finally:
        page.close()

# ---------------------------------------------------------------------------
# SCRAPING — BRAND LISTING PAGE
# ---------------------------------------------------------------------------

def scrape_brand_page(context, page_num=1):
    """Scrape one page of the Maybelline brand listing."""
    url = f"{BRAND_URL}?page={page_num}" if page_num > 1 else BRAND_URL
    html = fetch_page_html(context, url, wait_selector="a[href*='/products/']")
    if not html:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/products/[A-Za-z0-9]+/")):
        href = a["href"]
        m = re.search(r"/products/([A-Za-z0-9]+)/([^/?#]+)/?", href)
        if not m:
            continue
        qid  = m.group(1)
        slug = m.group(2)
        if qid in seen:
            continue
        seen.add(qid)

        # Title from link text or nearby heading
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            parent = a.find_parent("div") or a.find_parent("li")
            if parent:
                h = parent.find(["h2", "h3", "h4", "p"])
                if h:
                    title = h.get_text(strip=True)

        full_url = href if href.startswith("http") else BASE_URL + href
        products.append({
            "qid":   qid,
            "slug":  slug,
            "title": title,
            "url":   full_url,
        })

    # Check for next page
    has_next = bool(soup.find("a", href=re.compile(rf"[?&]page={page_num + 1}")))

    return products, has_next


def scrape_all_brand_products(context):
    """Scrape all pages of the Maybelline brand listing."""
    all_products = []
    page_num = 1
    while True:
        print(f"  Scraping brand page {page_num}...")
        products, has_next = scrape_brand_page(context, page_num)
        all_products.extend(products)
        print(f"    {len(products)} products (total: {len(all_products)})")
        if not has_next or not products:
            break
        page_num += 1
        time.sleep(REQUEST_DELAY + random.uniform(0, 2))
    return all_products

# ---------------------------------------------------------------------------
# SCRAPING — PRODUCT DETAIL PAGE
# ---------------------------------------------------------------------------

def scrape_product(context, product):
    """
    Scrape individual product page for:
    - GTIN / EAN
    - Lowest unit price
    - Total stock available
    - MOV (minimum order value)
    - Bundle size
    - Image
    """
    url = product["url"]
    html = fetch_page_html(context, url, wait_selector="table, [class*='offer'], [class*='supplier']")
    if not html:
        return product

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # GTIN / EAN
    gtin_m = re.search(r"GTIN[:\s]+([0-9]{8,14})", text)
    product["barcode"] = gtin_m.group(1) if gtin_m else ""

    # Image
    og_img = soup.find("meta", property="og:image")
    if og_img:
        product["image"] = og_img.get("content", "")

    # Title from h1
    h1 = soup.find("h1")
    if h1:
        product["title"] = h1.get_text(strip=True)

    # Parse all supplier offers: SUPPLIER_CODE  £unit_price  £MOV  stock
    # Format seen on page: "MRGEZY  £4.40  £10,000.00  26,820"
    offer_pattern = re.compile(
        r"([A-Z0-9]{5,8})\s+£([\d.]+)\s+£([\d,]+\.[\d]{2})\s+([\d,]+)"
    )
    offers = []
    for m in offer_pattern.finditer(text):
        offers.append({
            "supplier":  m.group(1),
            "unit_price": float(m.group(2)),
            "mov":        float(m.group(3).replace(",", "")),
            "stock":      int(m.group(4).replace(",", "")),
        })

    if offers:
        # Pick the supplier with the lowest unit price; break ties by lowest MOV
        best = sorted(offers, key=lambda o: (o["unit_price"], o["mov"]))[0]
        product["price"]       = f"{best['unit_price']:.2f}"
        product["mov"]         = f"{best['mov']:,.2f}"
        product["stock"]       = best["stock"]
        product["supplier"]    = best["supplier"]
        product["in_stock"]    = best["stock"] > 0
        product["all_offers"]  = len(offers)
    else:
        # Fallback: total stock from header
        stock_m = re.search(r"([\d,]+)\s+available", text)
        if stock_m:
            product["stock"]    = int(stock_m.group(1).replace(",", ""))
            product["in_stock"] = True
        elif "out of stock" in text.lower():
            product["stock"]    = 0
            product["in_stock"] = False
        else:
            product.setdefault("stock",    None)
            product.setdefault("in_stock", True)

    # Bundle size — "Bundles of X" (nearest to chosen supplier)
    bundle_m = re.search(r"[Bb]undles?\s+of\s+([\d,]+)", text)
    product["bundle_size"] = bundle_m.group(1).replace(",", "") if bundle_m else ""

    return product

# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def safe_float(val):
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    try:
        vat = f"{float(cost_price_str) * 1.2:.2f}"
    except (TypeError, ValueError):
        vat = cost_price_str
    return f"https://sas.selleramp.com/sas/lookup/?search_term={barcode}&sas_cost_price={vat}"

# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode     = product.get("barcode", "")
    stock       = product.get("stock")
    in_stock    = product.get("in_stock", True)
    mov         = product.get("mov", "")
    bundle_size = product.get("bundle_size", "")
    price       = product.get("price", "")
    sas_url     = selleramp_url(barcode, price)

    if stock is not None:
        stock_val = f"**{stock:,} units**"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    supplier   = product.get("supplier", "")
    all_offers = product.get("all_offers", "")

    fields = [
        {"name": "🔢 GTIN / EAN",    "value": f"`{barcode}`" if barcode else "-",              "inline": True},
        {"name": "📊 Stock (best)",   "value": stock_val,                                        "inline": True},
        {"name": "📦 Bundle Size",    "value": f"{bundle_size} units" if bundle_size else "-",  "inline": True},
        {"name": "💳 MOV (best)",     "value": f"£{mov}" if mov else "-",                       "inline": True},
        {"name": "🏭 Supplier",       "value": f"`{supplier}`" if supplier else "-",            "inline": True},
        {"name": "📋 Total Offers",   "value": str(all_offers) if all_offers else "-",          "inline": True},
    ]
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def notify_new(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Lowest Unit Price", "value": f"**£{price}**" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Maybelline Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, is_drop):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct   = f"{abs((new_f - old_f) / old_f * 100):.1f}%" if old_f and new_f else "?"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Change",    "value": f"{'↓' if is_drop else '↑'} {diff} ({pct})", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'💰  PRICE DROP' if is_drop else '📈  PRICE INCREASE'} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_PRICE_DROP if is_drop else COLOUR_PRICE_UP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Maybelline Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE {'DROP' if is_drop else 'UP'} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock, is_restock):
    diff = abs(new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "📊 Old Stock", "value": f"{old_stock:,} units" if isinstance(old_stock, int) else str(old_stock), "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock:,} units**" if isinstance(new_stock, int) else str(new_stock), "inline": True},
        {"name": "📉 Change",    "value": f"{'↑ +' if is_restock else '↓ -'}{diff:,}" if isinstance(diff, int) else str(diff), "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'🟢  RESTOCK' if is_restock else '📉  STOCK DROP'} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_RESTOCK if is_restock else COLOUR_LOW_STOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Maybelline Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: {'RESTOCK' if is_restock else 'STOCK DROP'} — {product.get('title', '')[:50]}")


def notify_oos(product):
    embed = {
        "title":     f"🔴  OUT OF STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_OOS,
        "fields":    _base_fields(product),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Maybelline Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: OOS — {product.get('title', '')[:60]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Lowest Unit Price", "value": f"**£{price}**" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Maybelline Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(data):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def snapshot_entry(product):
    return {
        "title":       product.get("title", ""),
        "url":         product.get("url", ""),
        "image":       product.get("image", ""),
        "barcode":     product.get("barcode", ""),
        "price":       product.get("price", ""),
        "stock":       product.get("stock"),
        "in_stock":    product.get("in_stock", True),
        "mov":         product.get("mov", ""),
        "bundle_size": product.get("bundle_size", ""),
        "supplier":    product.get("supplier", ""),
        "all_offers":  product.get("all_offers", ""),
        "first_seen":  product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    # Fill cached fields if scrape missed them
    for key in ("image", "barcode", "bundle_size", "mov"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
    elif was_in_stock and not now_in_stock:
        notify_oos(product)
        time.sleep(1)
    elif old_f and new_f and new_f < old_f - 0.01:
        notify_price_change(product, old_price, new_price, is_drop=True)
        time.sleep(1)
    elif old_f and new_f and new_f > old_f + 0.01:
        notify_price_change(product, old_price, new_price, is_drop=False)
        time.sleep(1)

    if old_stock is not None and new_stock is not None and now_in_stock:
        # Use 5% threshold to avoid noise from minor stock fluctuations
        threshold = max(50, int(old_stock * 0.05))
        if new_stock > old_stock + threshold:
            notify_stock_change(product, old_stock, new_stock, is_restock=True)
            time.sleep(1)
        elif new_stock < old_stock - threshold:
            notify_stock_change(product, old_stock, new_stock, is_restock=False)
            time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Qogita Maybelline...")

    with sync_playwright() as pw:
        browser, context = make_browser(pw)
        try:
            # Login first
            if not login(context):
                print("  [!] Cannot proceed without login")
                return

            snapshot     = load_snapshot()
            known_qids   = set(snapshot.keys())
            is_first_run = len(known_qids) == 0

            # Scrape all brand listing pages
            print("  Scraping Maybelline brand pages...")
            all_products = scrape_all_brand_products(context)
            if not all_products:
                print("  [!] No products found")
                return

            current_qids = {p["qid"] for p in all_products}
            new_qids     = current_qids - known_qids
            print(f"  {len(all_products)} products total, {len(new_qids)} new")

            for i, product in enumerate(all_products, 1):
                qid = product["qid"]
                print(f"  [{i}/{len(all_products)}] {product['title'][:55]}")
                time.sleep(REQUEST_DELAY + random.uniform(0, 2))

                # Scrape product detail for price/stock/GTIN
                product = scrape_product(context, product)

                if is_first_run:
                    # Silent baseline — no alerts
                    entry = snapshot_entry(product)
                    entry["first_seen"] = datetime.now(timezone.utc).isoformat()
                    snapshot[qid] = entry
                elif qid in new_qids:
                    notify_new(product)
                    time.sleep(1.5)
                    entry = snapshot_entry(product)
                    entry["first_seen"] = datetime.now(timezone.utc).isoformat()
                    snapshot[qid] = entry
                else:
                    old = snapshot[qid]
                    check_changes(product, old)
                    entry = snapshot_entry(product)
                    entry["first_seen"] = old.get("first_seen", entry["first_seen"])
                    snapshot[qid] = entry

                # Auto-save every 20 products
                if i % 20 == 0:
                    save_snapshot(snapshot)
                    print(f"  Auto-saved at {i}/{len(all_products)}")

            save_snapshot(snapshot)
            if is_first_run:
                print(f"  Baseline complete — {len(snapshot)} products. No alerts sent.")
            else:
                print(f"  Snapshot saved ({len(snapshot)} products tracked)")

        finally:
            browser.close()


def main():
    print("=" * 55)
    print("  Qogita Maybelline Monitor")
    print(f"  Watching: {BRAND_URL}")
    print("  Tracking: new listings, price drops, restocks")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()