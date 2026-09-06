import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BATCH_SIZE = 8
INDEX_FILE = Path("index.html")
DATA_FILE = Path("prices.json")
JST = timezone(timedelta(hours=9))

def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")

# ----------------------------
# 1) Read products in fixed order
# ----------------------------
soup = BeautifulSoup(INDEX_FILE.read_text(encoding="utf-8"), "html.parser")
products = []
seen = set()

for card in soup.select("article.card[data-amzn]"):
    url = (card.get("data-amzn") or "").strip()
    name = (card.get("data-name") or "").strip()
    if url and url not in seen:
        seen.add(url)
        products.append({"url": url, "name": name})

if not products:
    raise RuntimeError("No Amazon products found in index.html")

# ----------------------------
# 2) Load persistent cursor/state
# ----------------------------
if DATA_FILE.exists():
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
else:
    data = {}

items = data.get("items")
if not isinstance(items, dict):
    items = {}

meta = data.get("_meta")
if not isinstance(meta, dict):
    meta = {}

total = len(products)
start = int(meta.get("next_index", 0)) % total
batch_count = min(BATCH_SIZE, total)

selected = [products[(start + i) % total] for i in range(batch_count)]
next_index = (start + batch_count) % total
wrapped = start + batch_count >= total
cycle = int(meta.get("cycle", 0)) + (1 if wrapped else 0)

print(f"Total={total} start={start} next={next_index} cycle={cycle}")
print("Batch:", [p["name"] for p in selected])

# ----------------------------
# 3) Amazon price fetch helpers
# ----------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
})

unavailable_re = re.compile(
    r"現在お取り扱いできません|現在在庫切れ|一時的に在庫切れ|"
    r"在庫切れ|currently unavailable|temporarily out of stock",
    re.I,
)

yen_re = re.compile(
    r"(?:￥|¥)\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*円"
)

def normalize_price(text):
    if not text:
        return None
    m = yen_re.search(text.replace("\xa0", " "))
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").replace(",", "")
    try:
        value = int(raw)
    except Exception:
        return None
    return value if 300 <= value <= 500000 else None

def direct_amazon(short_url):
    """Try normal Amazon HTML first. Returns (result, final_url)."""
    try:
        r = session.get(short_url, allow_redirects=True, timeout=18)
        final_url = r.url
        text = r.text or ""

        # CAPTCHA / bot page: don't trust it.
        if re.search(r"captcha|enter the characters you see|ロボットではない", text, re.I):
            return None, final_url

        doc = BeautifulSoup(text, "html.parser")

        selectors = [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            ".reinventPricePriceToPayMargin .a-offscreen",
            "#price_inside_buybox",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            ".a-price.priceToPay .a-offscreen",
        ]

        for sel in selectors:
            el = doc.select_one(sel)
            if el:
                price = normalize_price(el.get_text(" ", strip=True))
                if price:
                    return {"status": "price", "price": price, "source": "amazon_html"}, final_url

        availability = ""
        for sel in ["#availability", "#outOfStock", "#availabilityInsideBuyBox_feature_div"]:
            el = doc.select_one(sel)
            if el:
                availability += " " + el.get_text(" ", strip=True)

        if unavailable_re.search(availability):
            return {"status": "unavailable", "source": "amazon_html"}, final_url

        return None, final_url
    except Exception as e:
        print(" direct error:", repr(e))
        return None, short_url

def parse_reader_text(text):
    """Parse Jina Reader markdown conservatively."""
    if not text:
        return None

    lines = [x.strip() for x in text.replace("\r", "").split("\n") if x.strip()]
    candidates = []

    for idx, line in enumerate(lines):
        matches = list(yen_re.finditer(line))
        if not matches:
            continue

        context = " ".join(lines[max(0, idx-2): min(len(lines), idx+3)])
        for m in matches:
            raw = (m.group(1) or m.group(2) or "").replace(",", "")
            try:
                value = int(raw)
            except Exception:
                continue
            if not (300 <= value <= 500000):
                continue

            score = 0
            if re.search(r"価格|Price|税込|新品|Amazon|在庫あり|カートに入れる|今すぐ買う", context, re.I):
                score += 8
            if re.search(r"参考価格|定価|過去価格|OFF|割引|クーポン|ポイント|中古|送料|配送料|月額|分割", context, re.I):
                score -= 9

            # Product price tends to be in the upper part of Reader output.
            if idx < 140:
                score += 2

            candidates.append((score, idx, value))

    candidates.sort(key=lambda x: (-x[0], x[1]))

    if candidates and candidates[0][0] >= 2:
        return {
            "status": "price",
            "price": candidates[0][2],
            "source": "jina_reader",
        }

    if unavailable_re.search(text):
        return {"status": "unavailable", "source": "jina_reader"}

    return None

def jina_fallback(final_url):
    try:
        # Reader fetches the target server-side; use the resolved Amazon URL.
        endpoint = "https://r.jina.ai/" + final_url
        r = session.get(endpoint, timeout=30)
        if not r.ok:
            print(" reader HTTP", r.status_code)
            return None
        return parse_reader_text(r.text)
    except Exception as e:
        print(" reader error:", repr(e))
        return None

def fetch_one(product):
    short_url = product["url"]
    result, final_url = direct_amazon(short_url)
    if result:
        return result

    # Give Amazon/Jina a short pause and then use fallback.
    time.sleep(1.0)
    return jina_fallback(final_url)

# ----------------------------
# 4) Process exactly the selected 8
# ----------------------------
batch_names = []
for n, product in enumerate(selected, 1):
    url = product["url"]
    name = product["name"]
    batch_names.append(name)
    print(f"[{n}/{batch_count}] {name}")

    old = items.get(url)
    if not isinstance(old, dict):
        old = {}

    attempt_at = now_iso()
    result = fetch_one(product)

    # Always advance state, but never erase last good price/state on fetch failure.
    if result:
        new_item = {
            **old,
            "name": name,
            "status": result["status"],
            "checked_at": attempt_at,
            "last_attempt_at": attempt_at,
            "last_attempt_status": "success",
            "source": result.get("source", ""),
        }
        if result["status"] == "price":
            new_item["price"] = int(result["price"])
        else:
            new_item.pop("price", None)
        items[url] = new_item
        print("  success:", result)
    else:
        if old:
            old.update({
                "name": name,
                "last_attempt_at": attempt_at,
                "last_attempt_status": "failed",
            })
            items[url] = old
        else:
            items[url] = {
                "name": name,
                "status": "unknown",
                "last_attempt_at": attempt_at,
                "last_attempt_status": "failed",
            }
        print("  failed; previous good data preserved")

    # Don't hammer either Amazon or the fallback service.
    time.sleep(2.0)

# ----------------------------
# 5) Persist cursor so the next run MUST continue from the next 8
# ----------------------------
data = {
    "_meta": {
        "next_index": next_index,
        "total": total,
        "batch_size": BATCH_SIZE,
        "cycle": cycle,
        "last_run": now_iso(),
        "last_batch_start": start,
        "last_batch_names": batch_names,
    },
    "items": items,
}

DATA_FILE.write_text(
    json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
    encoding="utf-8",
)

print("Saved next_index =", next_index)
