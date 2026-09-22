import os
import re
import json
import time
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

MONEX_URL = os.getenv(
    "MONEX_URL",
    "https://mst.monex.co.jp/pc/servlet/ITS/report/EconomyIndexCalendar"
)
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
NOTICE_MINUTES = int(os.getenv("NOTICE_MINUTES", "30"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TZ = ZoneInfo("Asia/Tokyo")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EconomicCalendarBot/1.0)"
}

# The Monex calendar currently exposes 18 regions and ★/★★/★★★ importance.
# This bot intentionally does not filter by importance or country.

def find_font(size):
    candidates = [
        os.getenv("FONT_PATH", ""),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
        "/Library/Fonts/NotoSansCJKjp-Regular.otf",
        "C:/Windows/Fonts/meiryo.ttc",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def normalize_cell(text):
    return re.sub(r"\s+", " ", text or "").strip()

def extract_country(cell):
    """Monex renders the country/region as an image on the current page.
    Try accessible text/attributes first, then fall back to image filename hints.
    """
    text = normalize_cell(cell.get_text(" ", strip=True))
    if text:
        return text
    for tag in cell.find_all(["img", "a"]):
        for attr in ("alt", "title", "aria-label"):
            value = normalize_cell(tag.get(attr, ""))
            if value:
                return value
        src = (tag.get("src", "") or "").lower()
        # Best-effort fallback for flag/icon filenames used by Monex.
        mapping = {
            "japan": "日本", "jp": "日本", "usa": "アメリカ", "us": "アメリカ",
            "america": "アメリカ", "euro": "欧州", "eu": "欧州", "europe": "欧州",
            "uk": "英国", "england": "英国", "germany": "ドイツ", "de": "ドイツ",
            "france": "フランス", "fr": "フランス", "australia": "豪州", "au": "豪州",
            "newzealand": "NZ", "nz": "NZ", "canada": "カナダ", "ca": "カナダ",
            "switzerland": "スイス", "ch": "スイス", "china": "中国", "cn": "中国",
            "hongkong": "香港", "hk": "香港", "india": "インド", "in": "インド",
            "brazil": "ブラジル", "br": "ブラジル", "southafrica": "南アフリカ", "za": "南アフリカ",
            "turkey": "トルコ", "tr": "トルコ", "korea": "韓国", "kr": "韓国",
            "singapore": "シンガポール", "sg": "シンガポール",
        }
        for key, name in mapping.items():
            if key in src:
                return name
    return "国・地域"

def parse_event_datetime(date_s, time_s, now):
    if not re.match(r"^\d{1,2}/\d{1,2}$", date_s):
        return None
    if not re.match(r"^\d{1,2}:\d{2}$", time_s):
        return None
    m, d = map(int, re.findall(r"\d+", date_s)[:2])
    hh, mm = map(int, time_s.split(":"))
    try:
        dt = datetime(now.year, m, d, hh, mm, tzinfo=TZ)
    except ValueError:
        return None
    # Handle a calendar crossing New Year.
    if dt < now - timedelta(days=180):
        dt = dt.replace(year=now.year + 1)
    return dt

def fetch_calendar():
    r = requests.get(MONEX_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    soup = BeautifulSoup(r.text, "html.parser")

    rows = []
    current_date = None
    now = datetime.now(TZ)

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 8:
            continue
        texts = [normalize_cell(c.get_text(" ", strip=True)) for c in cells]
        joined = " | ".join(texts)
        if "発表時刻" in joined or ("指標" in joined and "前回" in joined):
            continue

        # Current Monex layout: date, time, importance, country image,
        # indicator, previous, forecast, result, note.
        date_s = texts[0]
        if re.match(r"^\d{1,2}/\d{1,2}$", date_s):
            current_date = date_s
        elif current_date:
            date_s = current_date
        else:
            continue

        time_s = texts[1]
        importance = texts[2].replace(" ", "")
        if "★" not in importance:
            continue
        country = extract_country(cells[3]) if len(cells) >= 4 else "国・地域"
        indicator = texts[4] if len(texts) >= 5 else ""
        if not indicator:
            continue
        previous = texts[5] if len(texts) >= 6 else "-"
        forecast = texts[6] if len(texts) >= 7 else "-"
        result = texts[7] if len(texts) >= 8 else "-"
        note = texts[8] if len(texts) >= 9 else ""

        # Rows with '-' as the publication time are valid scheduled entries,
        # but cannot receive a 30-minute alert until Monex gives a time.
        dt = parse_event_datetime(date_s, time_s, now)

        stable_key = f"{date_s}|{time_s}|{country}|{indicator}"
        rows.append({
            "id": hashlib.sha1(stable_key.encode("utf-8")).hexdigest(),
            "datetime": dt.isoformat() if dt else None,
            "date": date_s,
            "time": time_s,
            "importance": importance,
            "country": country,
            "indicator": indicator,
            "previous": previous or "-",
            "forecast": forecast or "-",
            "result": result or "-",
            "note": note or "",
        })

    seen = set()
    out = []
    for x in rows:
        if x["id"] not in seen:
            seen.add(x["id"])
            out.append(x)
    return out

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"posted_day": {}, "posted_notice": {}, "posted_result": {}}

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def rounded_box(draw, xy, radius=22, fill=(25, 25, 32), outline=(125, 70, 220), width=3):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

def make_image(item, mode="event"):
    W, H = 1400, 820
    bg = (12, 12, 16)
    purple = (150, 80, 255)
    white = (245, 245, 248)
    gray = (170, 170, 180)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    title = find_font(48)
    h1 = find_font(54)
    h2 = find_font(40)
    body = find_font(34)
    small = find_font(27)

    # Header
    d.rectangle((0, 0, W, 12), fill=purple)
    d.text((70, 45), "ECONOMIC INDICATOR", font=title, fill=white)
    d.text((W-380, 58), item["date"], font=body, fill=gray)

    # Main card
    rounded_box(d, (55, 135, W-55, H-55))

    # Country / importance
    d.text((95, 175), item["country"], font=h2, fill=white)
    d.text((W-390, 178), item["importance"], font=h2, fill=purple)

    # Indicator
    indicator = item["indicator"]
    # Basic wrapping for long Japanese indicator names
    max_chars = 31
    lines = [indicator[i:i+max_chars] for i in range(0, len(indicator), max_chars)] or ["-"]
    y = 245
    for line in lines[:3]:
        d.text((95, y), line, font=h1 if len(lines) == 1 else h2, fill=white)
        y += 65

    # Time
    d.text((95, 455), f"発表時刻  {item['time']}", font=body, fill=purple)

    labels = [("前回", item["previous"]), ("予想", item["forecast"]), ("結果", item["result"])]
    x_positions = [95, 500, 905]
    for x, (label, value) in zip(x_positions, labels):
        d.text((x, 535), label, font=small, fill=gray)
        d.text((x, 575), value, font=body, fill=white)

    if mode == "notice":
        d.text((95, 665), f"発表まで約 {NOTICE_MINUTES} 分", font=body, fill=purple)
    elif mode == "result":
        d.text((95, 665), "RESULT UPDATED", font=body, fill=purple)
    else:
        d.text((95, 665), item["note"][:50], font=small, fill=gray)

    path = Path("generated")
    path.mkdir(exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", item["indicator"])[:45]
    out = path / f"{item['id']}_{mode}.png"
    img.save(out, "PNG", optimize=True)
    return out

def discord_send(image_path, content=None):
    with open(image_path, "rb") as f:
        files = {"file": (image_path.name, f, "image/png")}
        data = {}
        if content:
            data["content"] = content
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            data=data,
            files=files,
            timeout=20,
        )
    r.raise_for_status()

def main():
    state = load_state()
    print("Economic calendar bot started.")

    while True:
        try:
            now = datetime.now(TZ)
            items = fetch_calendar()
            today_key = now.strftime("%Y-%m-%d")

            # Daily: post all today's indicators as images.
            for item in items:
                if not item.get("datetime"):
                    continue
                dt = datetime.fromisoformat(item["datetime"])
                if dt.date() != now.date():
                    continue
                key = f"{today_key}:{item['id']}"
                if key not in state["posted_day"]:
                    image = make_image(item, "event")
                    discord_send(image)
                    state["posted_day"][key] = now.isoformat()
                    save_state(state)

            # 30-minute warning.
            for item in items:
                if not item.get("datetime"):
                    continue
                dt = datetime.fromisoformat(item["datetime"])
                seconds = (dt - now).total_seconds()
                key = f"{item['id']}:notice"
                if 0 <= seconds <= NOTICE_MINUTES * 60 and key not in state["posted_notice"]:
                    image = make_image(item, "notice")
                    discord_send(image)
                    state["posted_notice"][key] = now.isoformat()
                    save_state(state)

            # Result update: if result changes from "-" to a value, post result image.
            for item in items:
                if item["result"] in ("-", "", "―", "—"):
                    continue
                key = f"{item['id']}:result:{item['result']}"
                if key not in state["posted_result"]:
                    image = make_image(item, "result")
                    discord_send(image)
                    state["posted_result"][key] = now.isoformat()
                    save_state(state)

            # Keep state manageable.
            cutoff = now - timedelta(days=14)
            for section in ("posted_day", "posted_notice", "posted_result"):
                state[section] = {
                    k: v for k, v in state[section].items()
                    if datetime.fromisoformat(v) >= cutoff
                }
            save_state(state)

        except Exception as e:
            print("ERROR:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
