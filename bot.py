import os
import re
import json
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont


MONEX_URL = os.getenv(
    "MONEX_URL",
    "https://mxp2.monex.co.jp/pc/servlet/ITS/report/EconomyIndexCalendar"
)

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

NOTICE_MINUTES = 30
STATE_FILE = Path("state.json")

TZ = ZoneInfo("Asia/Tokyo")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.monex.co.jp/",
    "Connection": "keep-alive",
}


def find_font(size):
    fonts = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
    ]

    for font in fonts:
        if Path(font).exists():
            return ImageFont.truetype(font, size)

    return ImageFont.load_default()


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_country(cell):
    for tag in cell.find_all(["img", "a"]):

        for attr in ["alt", "title", "aria-label"]:
            value = normalize(tag.get(attr, ""))

            if value:
                return value

        src = (tag.get("src", "") or "").lower()

        mapping = {
            "japan": "日本",
            "jp": "日本",
            "usa": "アメリカ",
            "us": "アメリカ",
            "america": "アメリカ",
            "euro": "欧州",
            "eu": "欧州",
            "uk": "英国",
            "england": "英国",
            "germany": "ドイツ",
            "france": "フランス",
            "australia": "豪州",
            "newzealand": "NZ",
            "nz": "NZ",
            "canada": "カナダ",
            "switzerland": "スイス",
            "china": "中国",
            "hongkong": "香港",
            "india": "インド",
            "brazil": "ブラジル",
            "southafrica": "南アフリカ",
            "turkey": "トルコ",
            "korea": "韓国",
            "singapore": "シンガポール",
        }

        for key, name in mapping.items():
            if key in src:
                return name

    text = normalize(cell.get_text(" ", strip=True))

    return text if text else "国・地域"


def parse_datetime(date_s, time_s, now):

    if not re.match(r"^\d{1,2}/\d{1,2}$", date_s):
        return None

    if not re.match(r"^\d{1,2}:\d{2}$", time_s):
        return None

    month, day = map(
        int,
        date_s.split("/")
    )

    hour, minute = map(
        int,
        time_s.split(":")
    )

    try:
        dt = datetime(
            now.year,
            month,
            day,
            hour,
            minute,
            tzinfo=TZ
        )
    except ValueError:
        return None

    if dt < now - timedelta(days=180):
        dt = dt.replace(year=now.year + 1)

    return dt


def fetch_calendar():

    session = requests.Session()
    session.headers.update(HEADERS)

    # 先にMonexへアクセスしてセッションを作る
    session.get(
        "https://mxp2.monex.co.jp/",
        timeout=20
    )

    response = session.get(
        MONEX_URL,
        timeout=30
    )

    print("Monex status:", response.status_code)

    response.raise_for_status()

    response.encoding = response.apparent_encoding

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    now = datetime.now(TZ)

    rows = []
    current_date = None

    for tr in soup.find_all("tr"):

        cells = tr.find_all(["th", "td"])

        if len(cells) < 8:
            continue

        texts = [
            normalize(
                cell.get_text(
                    " ",
                    strip=True
                )
            )
            for cell in cells
        ]

        joined = " | ".join(texts)

        if "発表時刻" in joined:
            continue

        if "指標" in joined and "前回" in joined:
            continue

        date_s = texts[0]

        if re.match(
            r"^\d{1,2}/\d{1,2}$",
            date_s
        ):
            current_date = date_s

        elif current_date:
            date_s = current_date

        else:
            continue

        time_s = texts[1]
        importance = texts[2].replace(" ", "")

        if "★" not in importance:
            continue

        country = extract_country(
            cells[3]
        )

        indicator = texts[4]

        if not indicator:
            continue

        previous = (
            texts[5]
            if len(texts) > 5
            else "-"
        )

        forecast = (
            texts[6]
            if len(texts) > 6
            else "-"
        )

        result = (
            texts[7]
            if len(texts) > 7
            else "-"
        )

        note = (
            texts[8]
            if len(texts) > 8
            else ""
        )

        dt = parse_datetime(
            date_s,
            time_s,
            now
        )

        stable_key = (
            f"{date_s}|"
            f"{time_s}|"
            f"{country}|"
            f"{indicator}"
        )

        event_id = hashlib.sha1(
            stable_key.encode("utf-8")
        ).hexdigest()

        rows.append({
            "id": event_id,
            "datetime": (
                dt.isoformat()
                if dt
                else None
            ),
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

    unique = {}
    for item in rows:
        unique[item["id"]] = item

    print(
        "取得件数:",
        len(unique)
    )

    return list(unique.values())


def load_state():

    if not STATE_FILE.exists():
        return {
            "posted_notice": {},
            "posted_result": {}
        }

    try:
        state = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(state, dict):
            raise ValueError

        state.setdefault(
            "posted_notice",
            {}
        )

        state.setdefault(
            "posted_result",
            {}
        )

        return state

    except Exception:
        return {
            "posted_notice": {},
            "posted_result": {}
        }


def save_state(state):

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def make_image(item, mode):

    W = 1400
    H = 820

    bg = (12, 12, 16)
    purple = (150, 80, 255)
    white = (245, 245, 248)
    gray = (170, 170, 180)

    img = Image.new(
        "RGB",
        (W, H),
        bg
    )

    d = ImageDraw.Draw(img)

    title = find_font(48)
    h1 = find_font(54)
    h2 = find_font(40)
    body = find_font(34)
    small = find_font(27)

    d.rectangle(
        (0, 0, W, 12),
        fill=purple
    )

    d.text(
        (70, 45),
        "ECONOMIC INDICATOR",
        font=title,
        fill=white
    )

    d.text(
        (W - 380, 58),
        item["date"],
        font=body,
        fill=gray
    )

    d.rounded_rectangle(
        (55, 135, W - 55, H - 55),
        radius=22,
        fill=(25, 25, 32),
        outline=purple,
        width=3
    )

    d.text(
        (95, 175),
        item["country"],
        font=h2,
        fill=white
    )

    d.text(
        (W - 390, 178),
        item["importance"],
        font=h2,
        fill=purple
    )

    indicator = item["indicator"]

    max_chars = 31

    lines = [
        indicator[i:i + max_chars]
        for i in range(
            0,
            len(indicator),
            max_chars
        )
    ]

    if not lines:
        lines = ["-"]

    y = 245

    for line in lines[:3]:

        d.text(
            (95, y),
            line,
            font=(
                h1
                if len(lines) == 1
                else h2
            ),
            fill=white
        )

        y += 65

    d.text(
        (95, 455),
        f"発表時刻  {item['time']}",
        font=body,
        fill=purple
    )

    labels = [
        ("前回", item["previous"]),
        ("予想", item["forecast"]),
        ("結果", item["result"])
    ]

    positions = [
        95,
        500,
        905
    ]

    for x, (label, value) in zip(
        positions,
        labels
    ):

        d.text(
            (x, 535),
            label,
            font=small,
            fill=gray
        )

        d.text(
            (x, 575),
            value,
            font=body,
            fill=white
        )

    if mode == "notice":

        d.text(
            (95, 665),
            "発表まで約30分",
            font=body,
            fill=purple
        )

    elif mode == "result":

        d.text(
            (95, 665),
            "RESULT UPDATED",
            font=body,
            fill=purple
        )

    path = Path("generated")
    path.mkdir(
        exist_ok=True
    )

    output = (
        path /
        f"{item['id']}_{mode}.png"
    )

    img.save(
        output,
        "PNG"
    )

    return output


def send_discord(image_path):

    with open(
        image_path,
        "rb"
    ) as f:

        response = requests.post(
            DISCORD_WEBHOOK_URL,
            files={
                "file": (
                    image_path.name,
                    f,
                    "image/png"
                )
            },
            timeout=30
        )

    print(
        "Discord status:",
        response.status_code
    )

    response.raise_for_status()


def main():

    print(
        "=== Economic Calendar Bot ==="
    )

    now = datetime.now(TZ)

    print(
        "現在時刻:",
        now.isoformat()
    )

    items = fetch_calendar()

    state = load_state()

    # 30分前通知
    for item in items:

        if not item["datetime"]:
            continue

        dt = datetime.fromisoformat(
            item["datetime"]
        )

        seconds = (
            dt - now
        ).total_seconds()

        key = (
            item["id"] +
            ":notice"
        )

        if (
            0 <= seconds <= 1800
            and key not in state["posted_notice"]
        ):

            print(
                "30分前通知:",
                item["indicator"]
            )

            image = make_image(
                item,
                "notice"
            )

            send_discord(image)

            state["posted_notice"][key] = (
                now.isoformat()
            )

            save_state(state)

    # 結果発表後
    for item in items:

        result = item["result"]

        if result in [
            "",
            "-",
            "―",
            "—"
        ]:
            continue

        key = (
            item["id"] +
            ":result:" +
            result
        )

        if key in state["posted_result"]:
            continue

        print(
            "結果:",
            item["indicator"],
            result
        )

        image = make_image(
            item,
            "result"
        )

        send_discord(image)

        state["posted_result"][key] = (
            now.isoformat()
        )

        save_state(state)

    # 古い記録を削除
    cutoff = (
        now -
        timedelta(days=14)
    )

    for section in [
        "posted_notice",
        "posted_result"
    ]:

        cleaned = {}

        for key, value in state[section].items():

            try:
                saved = datetime.fromisoformat(
                    value
                )

                if saved >= cutoff:
                    cleaned[key] = value

            except Exception:
                pass

        state[section] = cleaned

    save_state(state)

    print(
        "=== 完了 ==="
    )


if __name__ == "__main__":
    main()
