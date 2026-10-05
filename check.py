"""警視庁 仮免許 予約サイトの空き枠を監視し、新しく空きが出たら ntfy でスマホに通知する。

環境変数:
  NTFY_TOPIC  通知先の ntfy トピック名（必須。未設定なら通知せず結果を表示するだけ）
  DATE_FROM   監視開始日 YYYYMMDD（既定: 20261015）
  DATE_TO     監視終了日 YYYYMMDD（既定: 20261031）
  PLACES      監視する試験場コード カンマ区切り（既定: 270,280 = 府中,鮫洲）
  LOOP_MINUTES      この分数のあいだ繰り返し確認する（既定: 0 = 1回だけ）
  INTERVAL_SECONDS  繰り返し時の確認間隔 秒（既定: 60）
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/calgetres"
SITE = "https://provisional.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/calendar/01/html/main.html?lang=ja"
COURSE_CODE = "19"  # 仮免許
PLACE_NAMES = {"270": "府中", "280": "鮫洲"}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
JST = timezone(timedelta(hours=9))

DATE_FROM = os.environ.get("DATE_FROM", "20261015")
DATE_TO = os.environ.get("DATE_TO", "20261031")
PLACES = os.environ.get("PLACES", "270,280").split(",")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))  # 0 なら1回だけ確認して終了
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))


def months_between(start, end):
    y, m = int(start[:4]), int(start[4:6])
    while f"{y}{m:02d}" <= end[:6]:
        yield f"{y}{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch(month, place):
    q = urllib.parse.urlencode({"date": month, "coursecode": COURSE_CODE, "placecode": place, "user": "pub"})
    req = urllib.request.Request(
        f"{API}?{q}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://provisional.tokyo-madoguchi-yoyaku.com",
            "Referer": "https://provisional.tokyo-madoguchi-yoyaku.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        data = json.load(res)
    if data.get("code") != "A0001":
        raise RuntimeError(f"unexpected response code: {data.get('code')}")
    return data["body"]


def find_vacancies():
    today = datetime.now(JST).strftime("%Y%m%d")
    start = max(DATE_FROM, today)
    found = {}
    for place in PLACES:
        for month in months_between(start, DATE_TO):
            for s in fetch(month, place):
                remain = int(s["capacity"]) - int(s["reservation"])
                if start <= s["date"] <= DATE_TO and remain > 0:
                    key = f"{place}-{s['date']}-{s['starttime']}"
                    d = s["date"]
                    am_pm = "午前" if s["starttime"] < "1000" else "午後"
                    found[key] = f"{PLACE_NAMES.get(place, place)} {int(d[4:6])}/{int(d[6:])} {am_pm} 残{remain}"
    return found


def notify(lines):
    payload = {
        "topic": NTFY_TOPIC,
        "title": "仮免許の予約に空きが出ました",
        "message": "\n".join(lines),
        "priority": 4,
        "tags": ["car"],
        "click": SITE,
    }
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def check_once():
    vacancies = find_vacancies()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            previous = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = set()

    new_keys = sorted(set(vacancies) - previous)
    print(f"[{datetime.now(JST):%H:%M:%S}] 空き枠: {len(vacancies)} 件 / 新規: {len(new_keys)} 件")
    for k in sorted(vacancies):
        print(("  [NEW] " if k in new_keys else "        ") + vacancies[k])

    if new_keys and NTFY_TOPIC:
        notify([vacancies[k] for k in new_keys])
        print("通知を送信しました")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(vacancies), f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    if LOOP_MINUTES <= 0:
        check_once()
        return
    deadline = time.monotonic() + LOOP_MINUTES * 60
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr, flush=True)
        sys.stdout.flush()
        if time.monotonic() + INTERVAL_SECONDS > deadline:
            break
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
