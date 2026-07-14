"""
Keepa API lookup module for Amazon UK product analysis.

Provides BSR, sales estimates, pricing, and FBA fees for a given
product, identified by EAN/GTIN barcode or title search.

Amazon UK referral fee schedule (Health & Beauty, 2026):
  - 8%  for items with a sale price of £10.00 or below
  - 15% for items with a sale price above £10.00
  Minimum referral fee: £0.30

Deps: pip install keepa requests
"""

import os
import keepa as keepa_lib

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")
DOMAIN        = "GB"   # Amazon UK

_client = None


def _get_client():
    global _client
    if _client is None:
        if not KEEPA_API_KEY:
            raise ValueError("KEEPA_API_KEY not set")
        _client = keepa_lib.Keepa(KEEPA_API_KEY)
    return _client


def lookup_by_ean(ean):
    """
    Look up a product on Amazon UK by EAN/GTIN barcode.
    Tries multiple EAN formats to maximise match rate:
      1. EAN as-is (e.g. 0000030147317)
      2. EAN with leading zeros stripped (e.g. 30147317)
      3. EAN zero-padded to 13 digits (standard EAN-13)
    Returns a parsed product dict or None if not found.
    """
    api = _get_client()

    # Build list of EAN variants to try
    ean_str = str(ean).strip()
    ean_stripped = ean_str.lstrip("0") or ean_str
    ean_padded   = ean_str.zfill(13) if len(ean_str) <= 13 else ean_str

    candidates = list(dict.fromkeys([ean_str, ean_padded, ean_stripped]))  # dedup, preserve order

    for candidate in candidates:
        try:
            products = api.query(
                candidate,
                product_code_is_asin=False,
                domain=DOMAIN,
                stats=90,
                history=True,
                rating=False,
            )
            if products:
                print(f"  Keepa match on EAN variant: {candidate}")
                return _parse_product(products[0])
        except Exception as e:
            return {"error": str(e)}

    return None


def lookup_by_title(title):
    """
    Search Amazon UK by product title via Keepa.
    Tries the full title first, then a shortened version (first 5 words)
    for better match rate on long Qogita product names.
    Returns the best-matching parsed product dict or None.
    """
    api = _get_client()

    # Try full title, then shortened version
    short_title = " ".join(title.split()[:6])
    titles_to_try = [title] if title == short_title else [title, short_title]

    for t in titles_to_try:
        try:
            asins = api.search_for_asins_by_title(t, domain=DOMAIN)
        except Exception as e:
            return {"error": str(e)}

        if not asins:
            continue

        # Fetch full data for top result
        try:
            full = api.query(
                asins[0],
                product_code_is_asin=True,
                domain=DOMAIN,
                stats=90,
                history=True,
                rating=False,
            )
            if full:
                print(f"  Keepa title match on: {t[:50]}")
                return _parse_product(full[0])
        except Exception as e:
            return {"error": str(e)}

    return None


def _price(current, idx):
    """Extract a price from Keepa's stats.current array (values are in pence)."""
    if current and idx < len(current) and current[idx] and current[idx] > 0:
        return current[idx] / 100.0
    return None


def _estimate_monthly_sales(product):
    """
    Estimate monthly sales using the most reliable available source.

    For high-volume products (BSR < 10,000), Keepa's monthlySold ML
    estimate is most accurate. For low-volume products, salesRankDrops30
    (actual rank changes = approx actual sales) is more reliable.

    Priority:
      1. monthlySold       — Keepa ML estimate, best for popular products
      2. salesRankDrops30  — actual rank drops last 30 days (≈ sales)
      3. salesRankDrops90/3 — 90-day average monthly approximation
    """
    stats = product.get("stats") or {}

    # Keepa's ML estimate — most accurate for bestsellers
    monthly_sold = product.get("monthlySold")
    if monthly_sold and monthly_sold > 0:
        return int(monthly_sold)

    # Fallback: actual rank drops in last 30 days
    drops30 = product.get("salesRankDrops30") or stats.get("salesRankDrops30")
    if drops30 and drops30 > 0:
        return int(drops30)

    # Last resort: 90-day drops averaged to monthly
    drops90 = product.get("salesRankDrops90") or stats.get("salesRankDrops90")
    if drops90 and drops90 > 0:
        return int(drops90 / 3)

    return None


def _parse_product(product):
    """Extract all the fields we need from a raw Keepa product dict."""
    stats   = product.get("stats") or {}
    current = stats.get("current") or []

    buybox_price  = _price(current, 10)
    amazon_price  = _price(current, 0)
    new_price     = _price(current, 1)   # 3rd-party new
    sell_price    = buybox_price or new_price or amazon_price

    # Sales rank — index 3 in current array
    sales_rank = None
    if len(current) > 3 and current[3] and current[3] > 0:
        sales_rank = current[3]

    # 30-day avg sales rank
    avg_rank_30 = stats.get("avg30", [None] * 4)
    avg_sales_rank_30 = avg_rank_30[3] if len(avg_rank_30) > 3 else None

    # FBA fees
    fba_fees      = product.get("fbaFees") or {}
    pick_pack_fee = fba_fees.get("pickAndPackFee")
    pick_pack_fee = pick_pack_fee / 100.0 if pick_pack_fee else None

    # Category
    category_tree = [c.get("name") for c in (product.get("categoryTree") or []) if c.get("name")]

    # 30-day and 90-day average buy box prices (index 10)
    avg30 = stats.get("avg30") or []
    avg90 = stats.get("avg90") or []
    buybox_avg30 = avg30[10] / 100.0 if len(avg30) > 10 and avg30[10] and avg30[10] > 0 else None
    buybox_avg90 = avg90[10] / 100.0 if len(avg90) > 10 and avg90[10] and avg90[10] > 0 else None

    # FBA seller count specifically — offerCountFba in stats
    # This counts sellers using FBA (Fulfilled by Amazon) only, which
    # is what matters for competition analysis
    fba_count = stats.get("offerCountFba")
    if fba_count is None:
        # Some products embed it at product level
        fba_count = product.get("offerCountFba")

    return {
        "asin":            product.get("asin"),
        "title":           product.get("title"),
        "sell_price":      sell_price,
        "buybox_price":    buybox_price,
        "buybox_avg30":    buybox_avg30,
        "buybox_avg90":    buybox_avg90,
        "amazon_price":    amazon_price,
        "sales_rank":      sales_rank,
        "avg_sales_rank_30": avg_sales_rank_30,
        "monthly_sales":   _estimate_monthly_sales(product),
        "fba_sellers":     fba_count,   # FBA-only seller count
        "fba_pick_pack":   pick_pack_fee,
        "category_tree":   category_tree,
        "amazon_url":      f"https://www.amazon.co.uk/dp/{product.get('asin')}" if product.get("asin") else None,
        "image_url":       (product.get("imagesCSV") or "").split(",")[0] if product.get("imagesCSV") else None,
    }
