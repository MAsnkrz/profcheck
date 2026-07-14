"""
Qogita Profit Analysis Bot

A Discord Gateway bot that watches your Qogita monitor channels and
automatically analyses every price-drop / new-listing / back-in-stock
embed using Keepa + a built-in Amazon UK profit calculator.

Only posts a reply if the product passes ALL configured filters:
  - BSR ≤ 100,000 (configurable)
  - Est. monthly sales ≥ 50 (configurable)
  - ROI ≥ 15% (configurable)
  - Profit ≥ £1.00 (configurable)

If the product fails any filter, it still posts a brief "filtered out"
reply explaining why — so you never wonder why something was skipped.

Deploy on Railway (free tier works fine — it just needs to stay alive):
  1. Push this folder to a GitHub repo
  2. railway.app → New Project → Deploy from repo
  3. Set env vars in Railway's Variables tab

Env vars:
  DISCORD_BOT_TOKEN        - from Discord Developer Portal
  KEEPA_API_KEY            - your Keepa access key
  MONITORED_CHANNEL_IDS    - comma-separated Discord channel IDs
  VAT_REGISTERED           - "true" (default) or "false"
  MAX_BSR                  - default 100000
  MIN_MONTHLY_SALES        - default 50
  MIN_ROI_PCT              - default 15.0
  MIN_PROFIT_GBP           - default 1.0

Deps: pip install discord.py keepa requests
"""

import os
import re
import asyncio
import requests
import discord
from urllib.parse import quote

import keepa_lookup
import profit_engine

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN     = os.getenv("DISCORD_BOT_TOKEN", "")
MONITORED_CHANNEL_IDS = {
    int(c.strip())
    for c in os.getenv("MONITORED_CHANNEL_IDS", "").split(",")
    if c.strip()
}

FILTERS = {
    "max_bsr":             int(os.getenv("MAX_BSR", "100000")),
    "min_monthly_sales":   int(os.getenv("MIN_MONTHLY_SALES", "50")),
    "min_roi_pct":         float(os.getenv("MIN_ROI_PCT", "15.0")),
    "min_profit_gbp":      float(os.getenv("MIN_PROFIT_GBP", "1.0")),
    "min_sellers":         int(os.getenv("MIN_SELLERS", "2")),
    "max_buybox_variance": float(os.getenv("MAX_BUYBOX_VARIANCE", "0.15")),
}

# Webhook to post passing leads to a separate Discord channel
CHECK_LEADS_WEBHOOK = os.getenv("CHECK_LEADS_WEBHOOK", "")

# Embed colours
COLOUR_PASS    = 0x00C853   # bright green — passes all filters
COLOUR_FAIL    = 0xE74C3C   # red — fails filters
COLOUR_NODATA  = 0x95A5A6   # grey — no Amazon listing found
COLOUR_ERROR   = 0xFF6B35   # orange — lookup error

# ---------------------------------------------------------------------------
# EMBED PARSING
# ---------------------------------------------------------------------------

def extract_embed_data(embed):
    """
    Pull barcode, cost price, and title from a Qogita monitor embed.
    Handles all field name variants across brand + category monitors.
    """
    barcode    = None
    cost_price = None
    title      = (embed.title or "").strip()

    # Strip emoji + prefix from title to get clean product name
    # e.g. "💵  PRICE DROP -12.3% — Maybelline Mascara" → "Maybelline Mascara"
    title_match = re.search(r"—\s*(.+)$", title)
    clean_title = title_match.group(1).strip() if title_match else title

    for field in (embed.fields or []):
        name_lower  = (field.name or "").lower()
        value       = (field.value or "")

        # Barcode / EAN / GTIN
        if barcode is None and any(k in name_lower for k in ("gtin", "ean", "barcode")):
            m = re.search(r"`?(\d{8,14})`?", value)
            if m:
                barcode = m.group(1)

        # New price — the cost we'll pay to Qogita
        if cost_price is None and "new price" in name_lower and "vat" not in name_lower:
            m = re.search(r"£\s*([\d.]+)", value)
            if m:
                cost_price = m.group(1)

    return barcode, cost_price, clean_title


def is_qogita_embed(embed):
    """
    Check if this embed is from one of our monitors.
    Covers: Qogita brand monitors, deep drop, Cocoon Centre, Notino.
    """
    footer = (embed.footer.text if embed.footer else "") or ""
    title  = (embed.title or "").upper()
    footer_lower = footer.lower()

    # Footer-based detection
    if any(k in footer_lower for k in ("qogita", "cocooncenter", "notino")):
        return True

    # Title-based detection (works even if footer format changes)
    if any(k in title for k in (
        "PRICE DROP", "NEW LISTING", "BACK IN STOCK", "DEEP DROP",
        "NEW ON SALE", "PASSES FILTERS", "COCOON CENTRE", "NOTINO SALE"
    )):
        return True

    return False

# ---------------------------------------------------------------------------
# DISCORD EMBED BUILDER
# ---------------------------------------------------------------------------

def _sas_title_url(title, cost_price):
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={quote(title)}&sas_cost_price={cost_price}"
    )


def _sas_ean_url(barcode, cost_price):
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={cost_price}"
    )


def send_to_leads_webhook(embed, original_embed):
    """
    Forward a passing product to the check-leads Discord channel
    via webhook. Includes the original Qogita alert embed + the
    profit analysis embed together in one message.
    """
    if not CHECK_LEADS_WEBHOOK:
        return
    try:
        payload = {
            "embeds": [
                {
                    "title":       original_embed.title,
                    "url":         original_embed.url,
                    "color":       original_embed.color.value if original_embed.color else 0,
                    "fields":      [{"name": f.name, "value": f.value, "inline": f.inline} for f in (original_embed.fields or [])],
                    "thumbnail":   {"url": original_embed.thumbnail.url} if original_embed.thumbnail else None,
                    "footer":      {"text": original_embed.footer.text} if original_embed.footer else None,
                    "timestamp":   original_embed.timestamp.isoformat() if original_embed.timestamp else None,
                },
                {
                    "title":   embed.title,
                    "url":     embed.url,
                    "color":   embed.color.value if embed.color else 0,
                    "fields":  [{"name": f.name, "value": f.value, "inline": f.inline} for f in (embed.fields or [])],
                    "thumbnail": {"url": embed.thumbnail.url} if embed.thumbnail else None,
                    "description": embed.description,
                }
            ]
        }
        # Remove None values from embeds
        payload["embeds"] = [
            {k: v for k, v in e.items() if v is not None}
            for e in payload["embeds"]
        ]
        r = requests.post(CHECK_LEADS_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            import time
            time.sleep(float(r.json().get("retry_after", 5)) + 0.5)
            requests.post(CHECK_LEADS_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
        print(f"  Sent to check-leads webhook")
    except Exception as e:
        print(f"  [!] Failed to send to check-leads webhook: {e}")


def build_pass_embed(barcode, cost_price, keepa_data, profit_data, clean_title):
    roi     = profit_data["roi_pct"]
    profit  = profit_data["profit"]
    monthly = keepa_data.get("monthly_sales")
    bsr     = keepa_data.get("sales_rank")

    fields = [
        # Amazon data
        {"name": "🛒 Amazon Price",   "value": f"£{profit_data['sell_price']:.2f}", "inline": True},
        {"name": "📊 BSR",            "value": f"{bsr:,}" if bsr else "-",           "inline": True},
        {"name": "📦 Est. Sales/mo",  "value": f"~{monthly:,}" if monthly else "-",  "inline": True},
        {"name": "🏪 Sellers",    "value": f"{keepa_data.get('fba_sellers', '-')}", "inline": True},
        {"name": "📈 BB 30d Avg",     "value": f"£{keepa_data['buybox_avg30']:.2f}" if keepa_data.get('buybox_avg30') else "-", "inline": True},
        {"name": "📈 BB 90d Avg",     "value": f"£{keepa_data['buybox_avg90']:.2f}" if keepa_data.get('buybox_avg90') else "-", "inline": True},
        # Cost & fees
        {"name": "💸 Qogita Cost (ex-VAT)", "value": f"£{profit_data['cost']/1.2:.2f}", "inline": True},
        {"name": "💸 Purchase Cost (inc-VAT)","value": f"£{profit_data['cost']:.2f}", "inline": True},
        {"name": "🏦 Referral Fee",            "value": f"£{profit_data['ref_fee']:.2f} ({profit_data['ref_pct']*100:.0f}%)", "inline": True},
        {"name": "📦 FBA Fee",                  "value": f"£{profit_data['fba_fee']:.2f}" if profit_data['fba_fee'] else "N/A", "inline": True},
        {"name": "💻 Digital Services",         "value": f"£{profit_data['dig_fee']:.2f}", "inline": True},
        {"name": "🧾 Total Fees",               "value": f"£{profit_data['total_fees']:.2f}", "inline": True},
        {"name": "🏛️ VAT on Fees (reclaim)",    "value": f"£{profit_data['vat_on_fees']:.2f}", "inline": True},
        {"name": "🏛️ VAT Due (HMRC)",           "value": f"£{profit_data['vat_due']:.2f}", "inline": True},
        {"name": "💰 Net Profit",     "value": f"**£{profit:.2f}**",    "inline": True},
        {"name": "📈 ROI",            "value": f"**{roi:.1f}%**",        "inline": True},
        {"name": "📐 Margin",         "value": f"{profit_data['margin_pct']:.1f}%", "inline": True},
        # ASIN
        {"name": "🔗 ASIN",           "value": f"[{keepa_data['asin']}]({keepa_data['amazon_url']})" if keepa_data.get("asin") else "-", "inline": True},
        # SAS links
        {"name": "🔍 SAS Title",      "value": f"[Search by title]({_sas_title_url(clean_title, str(round(profit_data['cost'],2)))})",    "inline": True},
        {"name": "🔍 SAS EAN",        "value": f"[Search by barcode]({_sas_ean_url(barcode, str(round(profit_data['cost'],2)))})" if barcode else "-", "inline": True},
    ]

    embed = discord.Embed(
        title=f"✅  PASSES FILTERS — {clean_title[:80]}",
        url=keepa_data.get("amazon_url"),
        color=COLOUR_PASS,
        description=(
            f"Filters: BSR ≤{FILTERS['max_bsr']:,} | "
            f"≥{FILTERS['min_monthly_sales']} sales/mo | "
            f"≥{FILTERS['min_roi_pct']}% ROI | "
            f"≥£{FILTERS['min_profit_gbp']} profit | "
            f"≥{FILTERS['min_sellers']} sellers | "
            f"≤{FILTERS['max_buybox_variance']*100:.0f}% BB variance"
        )
    )
    for f in fields:
        embed.add_field(**f)

    if keepa_data.get("image_url"):
        embed.set_thumbnail(url=keepa_data["image_url"])

    return embed


def build_fail_embed(barcode, cost_price, keepa_data, profit_data, clean_title, failure_reasons):
    bsr     = keepa_data.get("sales_rank") if keepa_data else None
    monthly = keepa_data.get("monthly_sales") if keepa_data else None
    roi     = profit_data["roi_pct"] if profit_data else None
    profit  = profit_data["profit"] if profit_data else None

    reasons_text = "\n".join(f"• {r}" for r in failure_reasons)

    fields = []
    if keepa_data:
        fields += [
            {"name": "🛒 Amazon Price",  "value": f"£{keepa_data['sell_price']:.2f}" if keepa_data.get("sell_price") else "-", "inline": True},
            {"name": "📊 BSR",           "value": f"{bsr:,}" if bsr else "-",          "inline": True},
            {"name": "📦 Est. Sales/mo", "value": f"~{monthly:,}" if monthly else "-", "inline": True},
        ]
    if profit_data:
        fields += [
            {"name": "💰 Profit",  "value": f"£{profit:.2f}" if profit else "-",  "inline": True},
            {"name": "📈 ROI",     "value": f"{roi:.1f}%" if roi else "-",         "inline": True},
        ]
    if barcode:
        fields.append({
            "name": "🔍 SAS EAN",
            "value": f"[Search by barcode]({_sas_ean_url(barcode, cost_price)})",
            "inline": True
        })

    embed = discord.Embed(
        title=f"❌  FILTERED OUT — {clean_title[:80]}",
        color=COLOUR_FAIL,
        description=f"**Why it was filtered:**\n{reasons_text}"
    )
    for f in fields:
        embed.add_field(**f)

    return embed


def build_nodata_embed(barcode, cost_price, clean_title, tried_title=False):
    desc = f"No Amazon UK listing found."
    if barcode:
        desc += f"\nBarcode tried: `{barcode}` (and zero-padded/stripped variants)"
    if tried_title:
        desc += f"\nTitle search also attempted — no match on Amazon UK."
    desc += f"\nProduct may not be listed on Amazon UK, or may be a bundle/multipack."

    fields = []
    if barcode:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({_sas_ean_url(barcode, cost_price)})", "inline": True})
    if clean_title:
        fields.append({"name": "🔍 SAS Title", "value": f"[Search by title]({_sas_title_url(clean_title, cost_price)})", "inline": True})

    embed = discord.Embed(
        title=f"🔍  NOT ON AMAZON — {clean_title[:80]}",
        color=COLOUR_NODATA,
        description=desc,
    )
    for f in fields:
        embed.add_field(**f)
    return embed


def build_error_embed(clean_title, error_msg):
    return discord.Embed(
        title=f"⚠️  Keepa lookup failed — {clean_title[:60]}",
        color=COLOUR_ERROR,
        description=f"Error: `{error_msg[:200]}`\nTry checking manually in SellerAmp.",
    )

# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def analyse_embed(message, embed):
    barcode, cost_price, clean_title = extract_embed_data(embed)

    if not cost_price:
        return  # can't calculate profit without a price

    print(f"  Analysing: {clean_title[:60]} | barcode={barcode} | cost=£{cost_price}")

    loop = asyncio.get_event_loop()

    # Step 1: Keepa lookup by barcode
    keepa_data = None
    tried_title = False

    if barcode:
        keepa_data = await loop.run_in_executor(None, keepa_lookup.lookup_by_ean, barcode)

    # Step 2: fallback to title search if barcode didn't work
    if keepa_data is None and clean_title:
        print(f"  No barcode match — trying title search...")
        tried_title = True
        keepa_data = await loop.run_in_executor(
            None, keepa_lookup.lookup_by_title, clean_title
        )

    # Step 3: handle error
    if keepa_data and keepa_data.get("error"):
        reply = build_error_embed(clean_title, keepa_data["error"])
        await message.reply(embed=reply, mention_author=False)
        return

    # Step 4: no Amazon listing at all
    if keepa_data is None:
        reply = build_nodata_embed(barcode, cost_price, clean_title, tried_title)
        await message.reply(embed=reply, mention_author=False)
        return

    # Step 5: profit calculation
    profit_data = profit_engine.calculate(cost_price, keepa_data)

    # Step 6: filter check
    passed, failures = profit_engine.passes_filters(profit_data, keepa_data, FILTERS)

    # Step 7: send to check-leads webhook if passes, silently ignore if fails
    if passed and profit_data:
        reply = build_pass_embed(barcode, cost_price, keepa_data, profit_data, clean_title)
        send_to_leads_webhook(reply, embed)
        print(f"  PASS: {clean_title[:50]} | ROI={profit_data['roi_pct']:.1f}% — sent to check-leads")
    else:
        print(f"  FAIL (ignored): {clean_title[:50]} | reasons: {', '.join(failures)}")


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print(f"Filters: {FILTERS}")
    print(f"Watching {len(MONITORED_CHANNEL_IDS)} channel(s): {MONITORED_CHANNEL_IDS}")


@client.event
async def on_message(message):
    # Only watch webhook messages (Qogita monitors post via webhook)
    if message.webhook_id is None:
        return

    if MONITORED_CHANNEL_IDS and message.channel.id not in MONITORED_CHANNEL_IDS:
        return

    if not message.embeds:
        return

    for embed in message.embeds:
        if is_qogita_embed(embed):
            await analyse_embed(message, embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("[!] DISCORD_BOT_TOKEN not set")
    client.run(DISCORD_BOT_TOKEN)
