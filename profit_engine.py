"""
Amazon UK profit calculator — correct VAT-inclusive formula.

Verified against SellerAmp with real data:
  Selling Price £7.80, Purchase Cost £2.20
  → Total Fees £2.14, VAT £0.93, Net Profit £2.53

Formula:
  Referral fee  = sell_price × referral_pct  (on VAT-inclusive price)
  FBA fee       = from Keepa fbaFees.pickAndPackFee
  Total fees    = referral_fee + fba_fee
  Net VAT       = (sell_price - cost) / 6    (output VAT - input VAT reclaim)
  Profit        = sell_price - cost - total_fees - net_vat
  ROI           = profit / cost × 100
  Margin        = profit / sell_price × 100

Both sell_price (Amazon Buy Box, VAT-inclusive UK consumer price) and
cost (Qogita new price as shown in embed) are treated as VAT-inclusive.
Net VAT represents what a VAT-registered seller pays to HMRC after
reclaiming input VAT on the purchase.

Amazon UK referral fee (Health & Beauty, 2026):
  ≤ £10.00 → 8%
  > £10.00 → 15%
  Minimum:   £0.30
"""

import os

MIN_REFERRAL = 0.30


def referral_fee_pct(sell_price, category_tree=None):
    cats = " ".join(category_tree or []).lower()
    is_hb = any(k in cats for k in [
        "health", "beauty", "personal care", "cosmetic",
        "fragrance", "hair", "skin", "baby", "wellness",
        "hygiene", "grooming", "oral", "deodorant"
    ])
    if is_hb:
        return 0.08 if sell_price <= 10.0 else 0.15
    return 0.15


def calculate(qogita_price_str, keepa_data):
    """
    Full profit calculation matching SellerAmp's output.

    qogita_price_str : the "New Price" from the Qogita embed (VAT-inclusive)
    keepa_data       : dict from keepa_lookup module
    """
    sell_price = keepa_data.get("sell_price")
    if not sell_price:
        return None

    try:
        cost = float(str(qogita_price_str).replace("£", "").replace(",", ""))
    except (TypeError, ValueError):
        return None

    ref_pct  = referral_fee_pct(sell_price, keepa_data.get("category_tree"))
    ref_fee  = max(sell_price * ref_pct, MIN_REFERRAL)
    fba_fee  = keepa_data.get("fba_pick_pack") or 0.0
    total_fees = ref_fee + fba_fee

    # Net VAT: output VAT on sale minus input VAT reclaim on purchase
    # Both prices treated as VAT-inclusive → (sell - cost) / 6
    net_vat = (sell_price - cost) / 6

    profit = sell_price - cost - total_fees - net_vat
    roi    = (profit / cost * 100)        if cost       > 0 else None
    margin = (profit / sell_price * 100)  if sell_price > 0 else None

    return {
        "sell_price":  sell_price,
        "cost":        cost,
        "ref_fee":     ref_fee,
        "ref_pct":     ref_pct,
        "fba_fee":     fba_fee,
        "total_fees":  total_fees,
        "net_vat":     net_vat,
        "profit":      profit,
        "roi_pct":     roi,
        "margin_pct":  margin,
    }


def passes_filters(profit_data, keepa_data, filters):
    """
    Returns (passed: bool, failures: list[str]).

    Filter keys (all optional):
      max_bsr           - max sales rank        (e.g. 100000)
      min_monthly_sales - min est. sales/month  (e.g. 50)
      min_roi_pct       - min ROI %             (e.g. 15.0)
      min_profit_gbp    - min profit £          (e.g. 1.0)
    """
    failures = []

    bsr     = keepa_data.get("sales_rank") if keepa_data else None
    monthly = keepa_data.get("monthly_sales") if keepa_data else None

    max_bsr = filters.get("max_bsr")
    if max_bsr is not None:
        if bsr is None:
            failures.append("No BSR data available")
        elif bsr > max_bsr:
            failures.append(f"BSR {bsr:,} > max {max_bsr:,}")

    min_sales = filters.get("min_monthly_sales")
    if min_sales is not None:
        if monthly is None:
            failures.append("No sales estimate available")
        elif monthly < min_sales:
            failures.append(f"Est. {monthly}/mo sales < min {min_sales}/mo")

    if profit_data:
        roi = profit_data.get("roi_pct")
        min_roi = filters.get("min_roi_pct")
        if min_roi is not None and roi is not None and roi < min_roi:
            failures.append(f"ROI {roi:.1f}% < min {min_roi}%")

        profit = profit_data.get("profit")
        min_p = filters.get("min_profit_gbp")
        if min_p is not None and profit is not None and profit < min_p:
            failures.append(f"Profit £{profit:.2f} < min £{min_p:.2f}")

    return len(failures) == 0, failures