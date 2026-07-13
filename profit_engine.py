"""
Amazon UK profit calculator — verified against Sellerfuse.

Formula (confirmed with real data, Selling £12.90, Cost £1.11 ex-VAT):
  cost_inc_vat     = qogita_price × 1.20  (Qogita prices are always ex-VAT)
  referral_fee     = sell_price × rate     (8% ≤£10, 15% >£10 for H&B)
  fba_fee          = from Keepa
  digital_services = sell_price × 0.7%    (Amazon regulatory/digital services fee)
  total_fees       = referral + fba + digital_services
  net_vat          = (sell_price - cost_inc_vat) / 6
  profit           = sell_price - cost_inc_vat - total_fees - net_vat

Sellerfuse result: Fees £4.79, VAT £1.98, Profit £4.80 (£0.05 VAT diff due
to internal rounding — acceptable for decision-making purposes).

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
        qogita_price_ex_vat = float(str(qogita_price_str).replace("£", "").replace(",", ""))
    except (TypeError, ValueError):
        return None

    # Qogita is a B2B platform — their prices are always ex-VAT.
    # The actual purchase cost is the inc-VAT price (ex-VAT × 1.20).
    # Net VAT formula: (sell_price_inc_vat - cost_inc_vat) / 6
    # = output VAT on Amazon sale minus input VAT reclaim on purchase.
    cost = qogita_price_ex_vat * 1.20   # inc-VAT cost (what you actually pay)

    ref_pct  = referral_fee_pct(sell_price, keepa_data.get("category_tree"))
    ref_fee  = max(sell_price * ref_pct, MIN_REFERRAL)
    fba_fee  = keepa_data.get("fba_pick_pack") or 0.0
    dig_fee  = round(sell_price * 0.007, 2)   # Amazon Digital Services / regulatory fee (0.7%)
    total_fees = ref_fee + fba_fee + dig_fee

    # SellerAmp VAT formula (confirmed from screenshot breakdown):
    #   VAT on Sale Price = sell_price / 6          (output VAT owed to HMRC)
    #   VAT on Cost Price = cost / 6                (input VAT reclaim on purchase)
    #   VAT on Fees       = total_fees × 20%        (input VAT reclaim on Amazon fees)
    #   VAT Due           = on_sale - on_cost - on_fees
    vat_on_sale = sell_price / 6
    vat_on_cost = cost / 6
    vat_on_fees = round(total_fees * 0.20, 2)
    vat_due     = vat_on_sale - vat_on_cost - vat_on_fees

    profit = sell_price - cost - total_fees - vat_on_fees - vat_due
    roi    = (profit / cost * 100)        if cost       > 0 else None
    margin = (profit / sell_price * 100)  if sell_price > 0 else None

    return {
        "sell_price":  sell_price,
        "cost":        cost,
        "ref_fee":     ref_fee,
        "ref_pct":     ref_pct,
        "fba_fee":     fba_fee,
        "dig_fee":     dig_fee,
        "total_fees":  total_fees,
        "vat_on_fees": vat_on_fees,
        "vat_due":     vat_due,
        "profit":      profit,
        "roi_pct":     roi,
        "margin_pct":  margin,
    }


def passes_filters(profit_data, keepa_data, filters):
    """
    Returns (passed: bool, failures: list[str]).

    Filter keys (all optional):
      max_bsr               - max sales rank              (e.g. 100000)
      min_monthly_sales     - min est. sales/month        (e.g. 50)
      min_roi_pct           - min ROI %                   (e.g. 15.0)
      min_profit_gbp        - min profit £                (e.g. 1.0)
      min_sellers           - min number of Amazon sellers (e.g. 2)
      max_buybox_variance   - max % diff between current buy box
                              and 30/90d average          (e.g. 0.15 = 15%)
    """
    failures = []

    bsr         = keepa_data.get("sales_rank") if keepa_data else None
    monthly     = keepa_data.get("monthly_sales") if keepa_data else None
    offer_count = keepa_data.get("offer_count") if keepa_data else None
    buybox      = keepa_data.get("buybox_price") or keepa_data.get("sell_price")
    bb_avg30    = keepa_data.get("buybox_avg30") if keepa_data else None
    bb_avg90    = keepa_data.get("buybox_avg90") if keepa_data else None

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

    # Only apply seller filter if Keepa returned the data
    if min_sellers is not None and offer_count is not None and offer_count < min_sellers:
        failures.append(f"Only {offer_count} seller(s) < min {min_sellers}")

    max_variance = filters.get("max_buybox_variance")
    if max_variance is not None and buybox and buybox > 0:
        if bb_avg30 and bb_avg30 > 0:
            v30 = abs(buybox - bb_avg30) / bb_avg30
            if v30 > max_variance:
                failures.append(f"Buy box vs 30d avg varies {v30*100:.0f}% > {max_variance*100:.0f}%")
        if bb_avg90 and bb_avg90 > 0:
            v90 = abs(buybox - bb_avg90) / bb_avg90
            if v90 > max_variance:
                failures.append(f"Buy box vs 90d avg varies {v90*100:.0f}% > {max_variance*100:.0f}%")

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
