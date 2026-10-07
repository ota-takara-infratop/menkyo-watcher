"""警視庁 仮免許 予約サイトの空き枠を監視し、新しく空きが出たら ntfy でスマホに通知する。
AUTO_BOOK=1 のときは空き枠を見つけたら最も早い枠を自動で予約し、1件予約できたら監視を止める。

環境変数:
  NTFY_TOPIC  通知先の ntfy トピック名（必須。未設定なら通知せず結果を表示するだけ）
  DATE_FROM   監視開始日 YYYYMMDD（既定: 20261015）
  DATE_TO     監視終了日 YYYYMMDD（既定: 20261031）
  PLACES      監視する試験場コード カンマ区切り（既定: 280 = 鮫洲。270 = 府中）
  LOOP_MINUTES      この分数のあいだ繰り返し確認する（既定: 0 = 1回だけ）
  INTERVAL_SECONDS  繰り返し時の確認間隔 秒（既定: 60）
  AUTO_BOOK         1 なら自動予約する（既定: 0）
  BOOK_NAME         予約者氏名（全角カタカナ。自動予約時に必須）
  BOOK_BIRTHDAY     生年月日 YYYYMMDD（自動予約時に必須）
  BOOK_PHONE        電話番号 ハイフンなし（自動予約時に必須）

個人情報と予約番号はログに出さない（公開リポジトリの Actions ログは誰でも見られるため）。
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/calgetres"
PUT_API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/putres"
SITE = "https://provisional.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index.html?lang=ja"  # 直接カレンダーを開くとリファラ確認で弾かれるため入口ページへ
COURSE_CODE = "19"  # 仮免許
PLACE_NAMES = {"270": "府中", "280": "鮫洲"}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
BOOKED_FILE = os.path.join(HERE, "booked.json")  # 予約できたら作る。あると監視しない
AUTOBOOK_STOPPED_FILE = os.path.join(HERE, "autobook_stopped.json")  # 想定外のエラーで自動予約を止めたら作る
JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://provisional.tokyo-madoguchi-yoyaku.com",
    "Referer": "https://provisional.tokyo-madoguchi-yoyaku.com/",
}

DATE_FROM = os.environ.get("DATE_FROM", "20261015")
DATE_TO = os.environ.get("DATE_TO", "20261031")
PLACES = os.environ.get("PLACES", "280").split(",")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))  # 0 なら1回だけ確認して終了
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))
AUTO_BOOK = os.environ.get("AUTO_BOOK") == "1"
BOOK_NAME = os.environ.get("BOOK_NAME", "")
BOOK_BIRTHDAY = os.environ.get("BOOK_BIRTHDAY", "")
BOOK_PHONE = os.environ.get("BOOK_PHONE", "")


class Booked(Exception):
    """予約が完了したので監視ループを終える"""


def months_between(start, end):
    y, m = int(start[:4]), int(start[4:6])
    while f"{y}{m:02d}" <= end[:6]:
        yield f"{y}{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch(month, place):
    q = urllib.parse.urlencode({"date": month, "coursecode": COURSE_CODE, "placecode": place, "user": "pub"})
    req = urllib.request.Request(f"{API}?{q}", headers=HEADERS)
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
                    slot = f"{PLACE_NAMES.get(place, place)} {int(d[4:6])}/{int(d[6:])} {am_pm}"
                    found[key] = {
                        "label": f"{slot} 残{remain}",
                        "slot": slot,
                        "place": place,
                        "date": d,
                        "starttime": s["starttime"],
                        "endtime": s.get("endtime", ""),
                    }
    return found


def notify(lines, title="仮免許の予約に空きが出ました"):
    if not NTFY_TOPIC:
        return
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
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


def normalize_name(name):
    # サイトの入力欄と同じく、ひらがな・半角カナは全角カタカナにし、空白は除いて大文字にする
    name = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in name)
    name = re.sub(r"[｡-ﾟ]+", lambda m: unicodedata.normalize("NFKC", m.group()), name)
    return "".join(name.split()).upper()


def put_reservation(v):
    """予約を送信する。成功ならレスポンスの dict、失敗ならエラーコード文字列を返す"""
    payload = {
        "date": v["date"],
        "coursecode": COURSE_CODE,
        "placecode": v["place"],
        "starttime": v["starttime"],
        "endtime": v["endtime"],
        "license": "",
        "phone": "".join(c for c in BOOK_PHONE if c.isdigit()),
        "birthday": BOOK_BIRTHDAY,
        "name": normalize_name(BOOK_NAME),
        "gracer_no": "",
    }
    req = urllib.request.Request(
        PUT_API,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**HEADERS, "Content-Type": "application/json; charset=UTF-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except Exception:
            return f"HTTP{e.code}"
    if data.get("status") == "OK":
        return data
    return str(data.get("code") or data.get("status") or "unknown")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")


def try_book(vacancies):
    if not AUTO_BOOK or not vacancies or os.path.exists(AUTOBOOK_STOPPED_FILE):
        return
    if not (BOOK_NAME and BOOK_BIRTHDAY and BOOK_PHONE):
        print("自動予約: 予約者情報（BOOK_*）が未設定のためスキップ")
        return

    # 最も早い枠を1つだけ予約する
    v = min(vacancies.values(), key=lambda x: (x["date"], x["starttime"]))
    result = put_reservation(v)

    if isinstance(result, dict):
        write_json(BOOKED_FILE, {"slot": v["slot"], "booked_at": datetime.now(JST).isoformat()})
        print(f"自動予約: 成功 {v['slot']}")
        notify(
            [
                f"{v['slot']} を予約しました",
                f"予約番号: {result.get('res_no', '')}",
                f"受付番号: {result.get('rec_no', '')}",
                "QRコードは予約サイトの「予約状況確認/キャンセル」で表示できます",
            ],
            title="仮免許の予約が完了しました",
        )
        raise Booked()

    if result == "B4002":  # 満席（他の人に先に取られた）。次の確認で再挑戦
        print(f"自動予約: 満席のため失敗 {v['slot']}")
        return

    # 既に予約がある・入力内容の誤りなど。同じ失敗を繰り返し送らないよう自動予約を止める
    write_json(AUTOBOOK_STOPPED_FILE, {"code": result, "at": datetime.now(JST).isoformat()})
    print(f"自動予約: エラー {result} のため自動予約を停止")
    notify(
        [
            f"自動予約がエラー（{result}）で失敗したため止めました",
            "既に予約が入っている可能性もあるので「予約状況確認/キャンセル」で確認してください",
            "空き通知は続けます",
        ],
        title="仮免許の自動予約が止まりました",
    )


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
        print(("  [NEW] " if k in new_keys else "        ") + vacancies[k]["label"])

    if new_keys and NTFY_TOPIC:
        notify([vacancies[k]["label"] for k in new_keys])
        print("通知を送信しました")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(vacancies), f, ensure_ascii=False, indent=1)
        f.write("\n")

    try_book(vacancies)


def main():
    if os.path.exists(BOOKED_FILE):
        print("予約済みのため監視しません（booked.json を消すと再開）")
        return
    if LOOP_MINUTES <= 0:
        try:
            check_once()
        except Booked:
            pass
        return
    deadline = time.monotonic() + LOOP_MINUTES * 60
    while True:
        try:
            check_once()
        except Booked:
            break
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
