"""警視庁 仮免許 予約サイトの空き枠を監視し、新しく空きが出たら ntfy でスマホに通知する。
AUTO_BOOK=1 のときは、予約がなければ最も早い空き枠を自動で予約する。
予約後も監視を続け、今の予約より早い「日」に空きが出たら、取り消して取り直す（乗り換え）。
現在の予約は STATE_KEY で暗号化して reservation.enc に保存する（乗り換えに失敗したら元の枠を取り直す）。

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
  STATE_KEY         reservation.enc の暗号化パスワード（自動予約時に必須。未設定なら自動予約しない）
  MIN_LEAD_DAYS     自動予約・乗り換えの対象を今日からこの日数以上先の枠に限る（既定: 7）

個人情報と予約番号はログに出さない（公開リポジトリの Actions ログは誰でも見られるため）。
"""
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/calgetres"
PUT_API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/putres"
CUSTOMER_API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/customerops"  # 予約の事前確認
CANCEL_API = "https://provisional-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/cancel"
SITE = "https://provisional.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index.html?lang=ja"  # 直接カレンダーを開くとリファラ確認で弾かれるため入口ページへ
COURSE_CODE = "19"  # 仮免許
PLACE_NAMES = {"270": "府中", "280": "鮫洲"}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
RESERVATION_FILE = os.path.join(HERE, "reservation.enc")  # 現在の予約（STATE_KEY で暗号化）。無ければ予約なし
SWITCH_MARKER_FILE = os.path.join(HERE, "switch_in_progress.json")  # 乗り換えの途中で作る目印（番号は入れない）。残っていたら自動予約を止める
SWITCH_SKIP_FILE = os.path.join(HERE, "switch_skip.json")  # 乗り換え先が満席で取れなかった枠のキー一覧。空きが消えるまで乗り換え先にしない
AUTOBOOK_STOPPED_FILE = os.path.join(HERE, "autobook_stopped.json")  # 想定外のエラーで自動予約を止めたら作る
JST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://provisional.tokyo-madoguchi-yoyaku.com",
    "Referer": "https://provisional.tokyo-madoguchi-yoyaku.com/",
}

DATE_FROM = os.environ.get("DATE_FROM", "20261106")
DATE_TO = os.environ.get("DATE_TO", "20261127")
PLACES = os.environ.get("PLACES", "280").split(",")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))  # 0 なら1回だけ確認して終了
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))
AUTO_BOOK = os.environ.get("AUTO_BOOK") == "1"
BOOK_NAME = os.environ.get("BOOK_NAME", "")
BOOK_BIRTHDAY = os.environ.get("BOOK_BIRTHDAY", "")
BOOK_PHONE = os.environ.get("BOOK_PHONE", "")
STATE_KEY = os.environ.get("STATE_KEY", "")
MIN_LEAD_DAYS = int(os.environ.get("MIN_LEAD_DAYS", "7"))  # 自動予約・乗り換えの対象は今日からこの日数以上先の枠


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
    today = today_jst()
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


def notify(lines, title="仮免許の予約に空きが出ました", priority=4):
    """ntfy に通知する。失敗しても例外は投げない（状態の保存を妨げないため）。送れたら True"""
    if not NTFY_TOPIC:
        return False
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": "\n".join(lines),
        "priority": priority,
        "tags": ["car"],
        "click": SITE,
    }
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"通知の送信に失敗 ({type(e).__name__})")
        return False
    return True


def normalize_name(name):
    # サイトの入力欄と同じく、ひらがな・半角カナは全角カタカナにし、空白は除いて大文字にする
    name = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in name)
    name = re.sub(r"[｡-ﾟ]+", lambda m: unicodedata.normalize("NFKC", m.group()), name)
    return "".join(name.split()).upper()


def today_jst():
    return datetime.now(JST).strftime("%Y%m%d")


def post_json(url, payload):
    """JSON を POST してレスポンスの dict を返す。HTTP エラーでも本文が JSON ならその dict、読めなければ code だけの dict"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**HEADERS, "Content-Type": "application/json; charset=UTF-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        # HTTP ステータスを残す（本文が JSON でも 5xx は結果不明として扱えるように）
        try:
            body = json.load(e)
        except Exception:
            body = None
        finally:
            e.close()
        code = body.get("code") if isinstance(body, dict) else None
        return {**(body if isinstance(body, dict) else {}), "code": code or f"HTTP{e.code}", "_http": e.code}


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
    data = post_json(PUT_API, payload)
    if data.get("_http", 0) >= 500:
        return f"HTTP{data['_http']}"  # サーバーエラーは本文に関係なく結果不明
    if data.get("status") == "OK":
        return data
    return str(data.get("code") or data.get("status") or "unknown")


def customer_op(url, res_no, action):
    """予約の事前確認（action=2）・キャンセル（action=3）を送信し、レスポンスの code を返す"""
    payload = {
        "res_no": res_no,
        "license": "",
        "phone": "".join(c for c in BOOK_PHONE if c.isdigit()),
        "birthday": BOOK_BIRTHDAY,
        "action": action,
        "coursecode": COURSE_CODE,
        "name": normalize_name(BOOK_NAME),
        "gracer_no": "",
    }
    data = post_json(url, payload)
    if data.get("_http", 0) >= 500:
        return f"HTTP{data['_http']}"
    return str(data.get("code") or "unknown")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")


def _openssl(extra_args, data):
    # パスワードは環境変数経由で渡す（コマンドラインに出さない）。openssl のエラー出力は表示しない
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-a", "-pass", "env:STATE_KEY", *extra_args]
    r = subprocess.run(cmd, input=data, capture_output=True, env={**os.environ, "STATE_KEY": STATE_KEY})
    if r.returncode != 0:
        raise RuntimeError("openssl failed")
    return r.stdout


def encrypt_text(text):
    return _openssl([], text.encode("utf-8")).decode("ascii")


def decrypt_text(text):
    return _openssl(["-d"], text.encode("ascii")).decode("utf-8")


def load_reservation():
    """現在の予約を返す。戻り値は (状態, 内容)。状態: "none"=予約なし / "ok" / "error"=復号できない"""
    if not os.path.exists(RESERVATION_FILE):
        return "none", None
    if not STATE_KEY:
        return "error", None
    try:
        with open(RESERVATION_FILE, encoding="utf-8") as f:
            data = json.loads(decrypt_text(f.read()))
        if not isinstance(data, dict) or not all(k in data for k in ("date", "starttime", "endtime", "place", "res_no")):
            return "error", None
        return "ok", data
    except Exception:
        return "error", None


def save_reservation(slot_info, result):
    """予約内容を暗号化して reservation.enc に保存する。保存できたら True"""
    data = {
        "date": slot_info["date"],
        "starttime": slot_info["starttime"],
        "endtime": slot_info["endtime"],
        "place": slot_info["place"],
        "slot": slot_info["slot"],
        "res_no": result.get("res_no", ""),
        "rec_no": result.get("rec_no", ""),
        "booked_at": datetime.now(JST).isoformat(),
    }
    if not STATE_KEY:
        print("警告: STATE_KEY が未設定のため予約内容を保存しません（乗り換えはできません）")
        return False
    try:
        enc = encrypt_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        print("警告: 予約内容の暗号化に失敗したため保存しません")
        return False
    with open(RESERVATION_FILE, "w", encoding="utf-8") as f:
        f.write(enc)
    return True


def delete_reservation():
    try:
        os.remove(RESERVATION_FILE)
    except FileNotFoundError:
        pass


def stop_autobook(code, lines, title="仮免許の自動予約が止まりました", priority=4):
    # 先に状態ファイルを書いてから通知する（通知が失敗しても止まった状態は残す）
    write_json(AUTOBOOK_STOPPED_FILE, {"code": code, "at": datetime.now(JST).isoformat()})
    print(f"自動予約: エラー {code} のため自動予約を停止")
    notify(lines, title=title, priority=priority)


def slot_of(r):
    if r.get("slot"):
        return r["slot"]
    d = r["date"]
    am_pm = "午前" if r["starttime"] < "1000" else "午後"
    return f"{PLACE_NAMES.get(r['place'], r['place'])} {int(d[4:6])}/{int(d[6:])} {am_pm}"


def save_or_stop(slot_info, result):
    """予約内容を保存する。保存できなければ reservation.enc を消して自動予約を止め、False を返す"""
    if result.get("res_no") and save_reservation(slot_info, result):
        return True
    delete_reservation()
    stop_autobook(
        "SAVE",
        [
            f"{slot_of(slot_info)} を予約しました",
            f"予約番号: {result.get('res_no', '')}",
            f"受付番号: {result.get('rec_no', '')}",
            "保存に失敗したため自動予約を止めました",
            "QRコードは予約サイトの「予約状況確認/キャンセル」で表示できます",
        ],
        title="仮免許の予約内容を保存できませんでした",
        priority=5,
    )
    return False


def load_switch_skip():
    try:
        with open(SWITCH_SKIP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [k for k in data if isinstance(k, str)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def skip_target(key):
    """満席などで取れなかった枠を記録する（その枠の空きが一度消えるまで予約・乗り換え先にしない）"""
    keys = load_switch_skip()
    if key not in keys:
        write_json(SWITCH_SKIP_FILE, sorted([*keys, key]))


def prune_switch_skip(vacancies):
    """今の空き枠に無いキーを記録から外す（消えてまた空いた枠は再び対象になる）。内容が変わるときだけ書く"""
    keys = load_switch_skip()
    kept = [k for k in keys if k in vacancies]
    if kept != keys:
        write_json(SWITCH_SKIP_FILE, kept)


# 予約送信の結果の分類（許可リスト方式）。成功 = status が OK、確定した失敗 = 下のコードだけ、それ以外はすべて「結果不明」。
# B4002 = 満席、B4001 / B4004 = その日は予約できない（サイト自身の画面もこの3つだけを扱う）
CONFIRMED_FAILURES = {"B4001", "B4002", "B4004"}


def safe_put(slot_info):
    """予約を送信し、成功なら dict、確定した失敗ならそのコード、それ以外（例外・5xx・未知のコード）は "EXCEPTION"（結果不明）を返す"""
    try:
        result = put_reservation(slot_info)
    except Exception as e:
        print(f"ERROR: put {type(e).__name__}")
        return "EXCEPTION"
    if isinstance(result, dict) or result in CONFIRMED_FAILURES:
        return result
    return "EXCEPTION"


def stop_unknown(text):
    """予約の結果が不明なとき: 保存済みの予約（取り消し済みの番号など）を消して自動予約を止める"""
    delete_reservation()
    stop_autobook("UNKNOWN", [text], title="仮免許の予約結果が不明です", priority=5)


def book_new(key, v):
    """予約がない状態から最も早い枠を予約する"""
    result = safe_put(v)
    if result == "EXCEPTION":
        stop_unknown("新規予約の結果が不明です。予約状況確認/キャンセルで確認してください。自動予約を止めました")
        return
    if isinstance(result, dict):
        if not save_or_stop(v, result):
            return
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
        return

    # 満席（他の人に先に取られた）・予約できない日。この枠は空きが消えるまで狙わず、次の確認で別の枠を試す
    print(f"自動予約: 予約できず（{result}） {v['slot']}")
    skip_target(key)


def switch_reservation(cur, v, key):
    """現在の予約 cur を、より早い日の空き枠 v に乗り換える（1回の確認につき1回だけ）"""
    old_slot = slot_of(cur)

    # 1. 取り消し前の確認（通信エラーは例外のまま上に投げ、次の確認で再挑戦）
    code = customer_op(CUSTOMER_API, cur["res_no"], "2")
    if code != "A1201":
        stop_autobook(
            code,
            [
                f"乗り換え前の確認がエラー（{code}）で失敗したので、取り消しも予約もしていません",
                f"今の予約はそのまま残っています（{old_slot}）",
                "空き通知は続けます",
            ],
            title="仮免許の自動乗り換えが止まりました",
        )
        return

    # 途中で落ちても分かるよう、取り消しの前に目印を書く（番号は入れない）。結果を保存し終えたら消す
    write_json(SWITCH_MARKER_FILE, {"old": old_slot, "new": v["slot"], "at": datetime.now(JST).isoformat()})
    _do_switch(cur, v, key, old_slot)
    try:
        os.remove(SWITCH_MARKER_FILE)
    except FileNotFoundError:
        pass


def _do_switch(cur, v, key, old_slot):
    # 2. 取り消し
    try:
        code = customer_op(CANCEL_API, cur["res_no"], "3")
    except Exception as e:
        print(f"ERROR: cancel {type(e).__name__}")
        code = "EXCEPTION"
    if code != "A1301":
        if re.fullmatch(r"[A-Z]\d{4}", code):  # サーバーが返した確定のエラーコード: 取り消されていない
            line = f"今の予約はそのまま残っています（{old_slot}）。念のため「予約状況確認/キャンセル」で確認してください"
        else:  # 例外・HTTPエラー・読めない応答: 取り消されたかどうか分からない
            line = f"取り消しの結果が不明です。予約状況確認/キャンセルで確認してください（{old_slot}）"
        stop_autobook(
            code,
            [f"取り消しがエラー（{code}）で失敗したので、新しい枠は予約していません", line, "空き通知は続けます"],
            title="仮免許の自動乗り換えが止まりました",
        )
        return

    # 3. 新しい枠を予約。以降は予約がない状態なので、失敗したら元の枠を取り直す
    result = safe_put(v)
    if isinstance(result, dict):
        if not save_or_stop(v, result):
            return
        print(f"自動予約: 乗り換え成功 {old_slot} -> {v['slot']}")
        notify(
            [
                f"{old_slot} → {v['slot']} に乗り換えました",
                f"予約番号: {result.get('res_no', '')}",
                f"受付番号: {result.get('rec_no', '')}",
                "QRコードは予約サイトの「予約状況確認/キャンセル」で表示できます",
            ],
            title="仮免許の予約を乗り換えました",
        )
        return

    if result == "EXCEPTION":
        # 結果が分からない（予約が通っているかもしれない）ので、元の枠は取り直さない
        stop_unknown(
            f"新しい枠の予約結果が不明です（{v['slot']}）。元の枠（{old_slot}）は取り消し済みです。"
            "予約状況確認/キャンセルで確認してください。自動予約を止めました"
        )
        return

    print(f"自動予約: 乗り換え先の予約に失敗（{result}） {v['slot']}、元の枠を取り直します")
    skip_target(key)
    old = {"date": cur["date"], "starttime": cur["starttime"], "endtime": cur["endtime"], "place": cur["place"], "slot": old_slot}
    back = safe_put(old)
    if back == "EXCEPTION":
        stop_unknown(
            f"元の枠の取り直し結果が不明です。元の枠（{old_slot}）は取り消し済みです（乗り換え先: {v['slot']}）。"
            "予約状況確認/キャンセルで確認してください。自動予約を止めました"
        )
        return
    if isinstance(back, dict):
        if not save_or_stop(old, back):
            return
        print(f"自動予約: 元の枠を取り直しました {old_slot}")
        notify(
            [
                f"{v['slot']} への乗り換えに失敗したので、元の枠（{old_slot}）を取り直しました",
                f"新しい予約番号: {back.get('res_no', '')}",
                f"新しい受付番号: {back.get('rec_no', '')}",
                "QRコードは予約サイトの「予約状況確認/キャンセル」で表示できます",
            ],
            title="仮免許の乗り換えに失敗しました",
        )
        return

    # 元の枠も満席などで取り直せなかった（確定の失敗）: 予約なしの状態。自動予約は続ける
    delete_reservation()
    print(f"自動予約: 元の枠も取り直せませんでした（{back}）。予約なしの状態")
    notify(
        [
            f"乗り換えに失敗し、元の枠（{old_slot}）も取り直せませんでした（{back}）",
            "今は予約がありません。自動予約を続けます",
        ],
        title="仮免許の予約がなくなりました",
        priority=5,
    )


def try_book(vacancies):
    if not AUTO_BOOK or os.path.exists(AUTOBOOK_STOPPED_FILE):
        return
    if not (BOOK_NAME and BOOK_BIRTHDAY and BOOK_PHONE):
        print("自動予約: 予約者情報（BOOK_*）が未設定のためスキップ")
        return
    if not STATE_KEY:
        print("警告: STATE_KEY が未設定のため自動予約をしません")
        return
    if os.path.exists(SWITCH_MARKER_FILE):
        stop_autobook(
            "INTERRUPTED",
            ["乗り換えの途中で中断された可能性があります。予約状況確認/キャンセルで確認してください", "自動予約を止めました"],
            priority=5,
        )
        return
    if not vacancies:
        return

    # 予約・乗り換えの対象は、今日から MIN_LEAD_DAYS 日以上先で、取れなかった記録のない枠だけ（空き通知は全部の枠が対象）
    min_date = (datetime.strptime(today_jst(), "%Y%m%d") + timedelta(days=MIN_LEAD_DAYS)).strftime("%Y%m%d")
    skipped = set(load_switch_skip())
    targets = {k: x for k, x in vacancies.items() if x["date"] >= min_date and k not in skipped}
    if not targets:
        return

    state, cur = load_reservation()
    if state == "error":
        stop_autobook(
            "DECRYPT",
            [
                "予約内容（reservation.enc）を読めないため自動予約を止めました",
                "STATE_KEY が正しいか確認してください。今の予約は「予約状況確認/キャンセル」で確認できます",
            ],
        )
        return

    if state == "none":
        # 最も早い枠を1つだけ予約する
        k = min(targets, key=lambda k: (targets[k]["date"], targets[k]["starttime"]))
        book_new(k, targets[k])
        return

    # 予約あり: 予約日が今日以前なら何もしない。そうでなければ、より早い「日」の枠があるときだけ乗り換える
    if cur["date"] <= today_jst():
        return
    earlier = {k: x for k, x in targets.items() if x["date"] < cur["date"]}
    if not earlier:
        return
    k = min(earlier, key=lambda k: (earlier[k]["date"], earlier[k]["starttime"]))
    switch_reservation(cur, earlier[k], k)


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
        if notify([vacancies[k]["label"] for k in new_keys]):
            print("通知を送信しました")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(vacancies), f, ensure_ascii=False, indent=1)
        f.write("\n")

    prune_switch_skip(vacancies)
    try_book(vacancies)


def main():
    if today_jst() > DATE_TO:
        print("監視期間を過ぎたので何もしません")
        return
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
