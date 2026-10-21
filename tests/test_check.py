"""check.py の自動予約・乗り換えのテスト。ネットワークは使わない（post_json / fetch / notify を差し替える）。
実行: python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check  # noqa: E402

NAME, BIRTH, PHONE = "タナカ タロウ", "19900101", "090-1234-5678"
SECRETS = ["RES-OLD-111", "REC-OLD-222", "RES-NEW-333", "REC-NEW-444", "RES-BACK-555", "REC-BACK-666",
           "タナカ", "タロウ", "19900101", "09012345678", "090-1234-5678"]
TODAY = "20261101"
REAL_NOTIFY = check.notify
REAL_POST_JSON = check.post_json


def vac(date, start="0800", end="0900"):
    d = date
    am_pm = "午前" if start < "1000" else "午後"
    return {"slot": f"鮫洲 {int(d[4:6])}/{int(d[6:])} {am_pm}", "label": "", "place": "280",
            "date": d, "starttime": start, "endtime": end}


def vs(*vacancies):
    """実際の空き枠のキー（場所-日付-開始時刻）を付けた dict にする"""
    return {f"{v['place']}-{v['date']}-{v['starttime']}": v for v in vacancies}


def ok(res, rec):
    return {"status": "OK", "code": "A0001", "res_no": res, "rec_no": rec}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.calls = []       # (url, payload)
        self.responses = {}   # url -> list of dict / Exception
        self.notices = []     # (title, lines, priority)
        self.stopped = os.path.join(t, "autobook_stopped.json")
        self.resv = os.path.join(t, "reservation.enc")
        self.marker = os.path.join(t, "switch_in_progress.json")
        self.snaps = []       # notify 時点の状態ファイルの有無 (reservation.enc, stopped, marker)
        for p in [
            mock.patch.object(check, "RESERVATION_FILE", self.resv),
            mock.patch.object(check, "AUTOBOOK_STOPPED_FILE", self.stopped),
            mock.patch.object(check, "STATE_FILE", os.path.join(t, "state.json")),
            mock.patch.object(check, "SWITCH_MARKER_FILE", self.marker, create=True),
            mock.patch.object(check, "MIN_LEAD_DAYS", 7, create=True),
            mock.patch.object(check, "SWITCH_SKIP_FILE", os.path.join(t, "switch_skip.json"), create=True),
            mock.patch.object(check, "AUTO_BOOK", True),
            mock.patch.object(check, "BOOK_NAME", NAME),
            mock.patch.object(check, "BOOK_BIRTHDAY", BIRTH),
            mock.patch.object(check, "BOOK_PHONE", PHONE),
            mock.patch.object(check, "STATE_KEY", "test-key-1"),
            mock.patch.object(check, "today_jst", lambda: TODAY),
            mock.patch.object(check, "post_json", self.fake_post),
            mock.patch.object(check, "notify", self.fake_notify),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def fake_post(self, url, payload):
        self.calls.append((url, payload))
        r = self.responses[url].pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def fake_notify(self, lines, title="x", priority=4):
        self.notices.append((title, list(lines), priority))
        self.snaps.append((os.path.exists(self.resv), os.path.exists(self.stopped), os.path.exists(self.marker)))

    def queue(self, url, *resps):
        self.responses[url] = list(resps)

    def urls(self):
        return [u.rsplit("/", 1)[1] for u, _ in self.calls]

    def run_quiet(self, vacancies):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            check.try_book(vacancies)
        text = out.getvalue() + err.getvalue()
        for s in SECRETS:
            self.assertNotIn(s, text, f"出力に機密 {s!r} が含まれる")
        return text

    def put_reservation_file(self, date="20261120", key=None):
        with mock.patch.object(check, "STATE_KEY", key or check.STATE_KEY):
            check.save_reservation(
                {"date": date, "starttime": "0800", "endtime": "0900", "place": "280", "slot": "鮫洲 11/20 午前"},
                ok("RES-OLD-111", "REC-OLD-222"))

    def skip_json(self):
        with open(os.path.join(self.tmp.name, "switch_skip.json"), encoding="utf-8") as f:
            return json.load(f)

    def stopped_json(self):
        with open(self.stopped, encoding="utf-8") as f:
            return json.load(f)

    def read_reservation(self):
        state, data = check.load_reservation()
        self.assertEqual(state, "ok")
        return data


class TestBooking(Base):
    def test_a_no_reservation_books_earliest_and_saves_encrypted(self):
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261110", "1300", "1400"), "b": vac("20261108"), "c": vac("20261108", "1000", "1100")})
        self.assertEqual(self.calls[0][1]["date"], "20261108")
        self.assertEqual(self.calls[0][1]["starttime"], "0800")
        with open(self.resv, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("RES-NEW-333", raw)
        self.assertNotIn("20261108", raw)
        d = self.read_reservation()
        self.assertEqual((d["date"], d["res_no"], d["rec_no"], d["slot"]), ("20261108", "RES-NEW-333", "REC-NEW-444", "鮫洲 11/8 午前"))
        self.assertEqual(d["place"], "280")
        self.assertEqual(self.notices[0][0], "仮免許の予約が完了しました")
        self.assertFalse(os.path.exists(self.stopped))

    def test_b_full_is_skipped_and_other_slot_tried(self):
        self.queue(check.PUT_API, {"status": "NG", "code": "B4002"}, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet(vs(vac("20261108"), vac("20261110")))
        self.assertFalse(os.path.exists(self.resv))
        self.assertFalse(os.path.exists(self.stopped))
        self.assertEqual(self.skip_json(), ["280-20261108-0800"])
        self.run_quiet(vs(vac("20261108"), vac("20261110")))  # 満席だった枠は飛ばして次に早い枠を試す
        self.assertEqual(self.calls[1][1]["date"], "20261110")
        self.assertEqual(self.read_reservation()["date"], "20261110")

    def test_new_booking_skip_cleared_when_slot_reappears(self):
        self.queue(check.PUT_API, {"status": "NG", "code": "B4002"}, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet(vs(vac("20261108")))
        self.run_quiet(vs(vac("20261108")))
        self.assertEqual(len(self.calls), 1)  # 同じ枠は狙わない
        check.prune_switch_skip({})  # 枠が消えて
        self.run_quiet(vs(vac("20261108")))  # また空いたら再挑戦
        self.assertEqual(len(self.calls), 2)

    def test_unknown_code_on_new_booking_stops(self):
        self.queue(check.PUT_API, {"status": "NG", "code": "B9999"})
        self.run_quiet(vs(vac("20261108")))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertFalse(os.path.exists(self.resv))
        self.assertEqual(self.notices[0][2], 5)
        self.assertIn("新規予約の結果が不明です", "".join(self.notices[0][1]))
        self.run_quiet(vs(vac("20261108")))  # 止まった後は何も送らない
        self.assertEqual(len(self.calls), 1)

    def test_i_state_key_missing_no_autobook(self):
        with mock.patch.object(check, "STATE_KEY", ""):
            text = self.run_quiet({"a": vac("20261108")})
            self.assertIn("警告", text)
            self.assertEqual(self.calls, [])
            self.assertFalse(os.path.exists(self.resv))
            self.assertFalse(os.path.exists(self.stopped))
            text = self.run_quiet({"a": vac("20261108")})  # 毎回警告する
            self.assertIn("警告", text)

    def test_lead_time_booking(self):
        # today=20261101、7日後は 20261108。前日までの枠は予約しない
        self.run_quiet({"a": vac("20261107"), "b": vac("20261103")})
        self.assertEqual(self.calls, [])
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261107"), "b": vac("20261108", "1300", "1400")})
        self.assertEqual(self.calls[0][1]["date"], "20261108")

    def test_notify_failure_does_not_block_state(self):
        # 本物の notify は失敗しても例外を投げない
        with mock.patch.object(check, "NTFY_TOPIC", "t"),                 mock.patch.object(check.urllib.request, "urlopen", side_effect=OSError("down")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(REAL_NOTIFY(["x"]))
            self.assertIn("通知の送信に失敗", out.getvalue())

    def test_j_wrong_key_stops(self):
        self.put_reservation_file()
        with mock.patch.object(check, "STATE_KEY", "wrong-key"):
            self.run_quiet({"a": vac("20261108")})
        self.assertEqual(self.stopped_json()["code"], "DECRYPT")
        self.assertTrue(os.path.exists(self.resv))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.notices), 1)

    def test_garbage_file_stops(self):
        with open(self.resv, "w") as f:
            f.write("not encrypted\n")
        self.run_quiet({"a": vac("20261108")})
        self.assertTrue(os.path.exists(self.stopped))
        self.assertEqual(self.calls, [])


class TestSwitch(Base):
    def setUp(self):
        super().setUp()
        self.put_reservation_file("20261120")

    def test_c_no_action_for_later_or_same_date(self):
        self.run_quiet({"a": vac("20261120", "1300", "1400"), "b": vac("20261120"), "c": vac("20261125")})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notices, [])
        self.run_quiet({})
        self.assertEqual(self.calls, [])

    def test_d_switch_success(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261110", "1300", "1400"), "b": vac("20261109"), "c": vac("20261109", "1000", "1100")})
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres"])
        (_, p2), (_, p3), (_, p1) = self.calls
        self.assertEqual((p2["action"], p3["action"]), ("2", "3"))
        for p in (p2, p3):
            self.assertEqual(p["res_no"], "RES-OLD-111")
            self.assertEqual(p["phone"], "09012345678")
            self.assertEqual(p["name"], "タナカタロウ")
            self.assertEqual(p["birthday"], BIRTH)
            self.assertEqual(p["coursecode"], "19")
        self.assertEqual((p1["date"], p1["starttime"]), ("20261109", "0800"))
        d = self.read_reservation()
        self.assertEqual((d["date"], d["res_no"], d["rec_no"]), ("20261109", "RES-NEW-333", "REC-NEW-444"))
        title, lines, _ = self.notices[0]
        self.assertEqual(title, "仮免許の予約を乗り換えました")
        self.assertIn("鮫洲 11/20 午前", "".join(lines))
        self.assertIn("鮫洲 11/9 午前", "".join(lines))
        self.assertIn("RES-NEW-333", "".join(lines))

    def test_e_precheck_failure_stops_and_keeps(self):
        self.queue(check.CUSTOMER_API, {"code": "A9999"})
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.urls(), ["customerops"])
        self.assertEqual(self.stopped_json()["code"], "A9999")
        self.assertEqual(self.read_reservation()["res_no"], "RES-OLD-111")
        self.assertEqual(self.notices[0][0], "仮免許の自動乗り換えが止まりました")

    def test_cancel_failure_stops_and_keeps(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A9998"})
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.urls(), ["customerops", "cancel"])
        self.assertEqual(self.stopped_json()["code"], "A9998")
        self.assertEqual(self.read_reservation()["res_no"], "RES-OLD-111")

    def test_f_new_fails_old_rebooked(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        self.queue(check.PUT_API, {"status": "NG", "code": "B4002"}, ok("RES-BACK-555", "REC-BACK-666"))
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres", "putres"])
        self.assertEqual(self.calls[3][1]["date"], "20261120")
        d = self.read_reservation()
        self.assertEqual((d["date"], d["slot"], d["res_no"], d["rec_no"]), ("20261120", "鮫洲 11/20 午前", "RES-BACK-555", "REC-BACK-666"))
        self.assertEqual(self.notices[0][0], "仮免許の乗り換えに失敗しました")
        self.assertIn("RES-BACK-555", "".join(self.notices[0][1]))
        self.assertFalse(os.path.exists(self.stopped))

    def test_g_both_fail_b4002_removes_reservation(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        self.queue(check.PUT_API, {"status": "NG", "code": "B4002"}, {"status": "NG", "code": "B4002"})
        self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.resv))
        self.assertFalse(os.path.exists(self.stopped))
        self.assertEqual(self.notices[0][0], "仮免許の予約がなくなりました")
        self.assertEqual(self.notices[0][2], 5)
        # 次の確認では予約なしとして通常予約に戻る
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet(vs(vac("20261115")))
        self.assertEqual(self.read_reservation()["date"], "20261115")

    def switch_queues(self, *put):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        self.queue(check.PUT_API, *put)

    def test_unknown_code_on_rebook_is_unknown_outcome(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, {"status": "NG", "code": "B7777"})
        self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.resv))
        self.assertEqual(self.stopped_json()["code"], "UNKNOWN")
        text = "".join(self.notices[0][1])
        self.assertIn("元の枠の取り直し結果が不明です", text)
        self.assertIn("元の枠（鮫洲 11/20 午前）は取り消し済みです", text)
        self.assertIn("鮫洲 11/9 午前", text)
        self.assertNotIn("自動予約を続けます", text)
        self.assertEqual(len(self.notices), 1)

    def test_both_fail_b4002_wording_continues(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, {"status": "NG", "code": "B4002"})
        self.run_quiet({"a": vac("20261109")})
        self.assertIn("自動予約を続けます", "".join(self.notices[0][1]))
        self.assertNotIn("止めました", "".join(self.notices[0][1]))

    def test_exception_in_new_put_unknown_outcome(self):
        self.switch_queues(OSError("timeout"))
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres"])  # 元の枠は取り直さない
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        title, lines, prio = self.notices[0]
        self.assertEqual(prio, 5)
        text = "".join(lines)
        self.assertIn("新しい枠の予約結果が不明です", text)
        self.assertIn("元の枠（鮫洲 11/20 午前）は取り消し済みです", text)
        self.assertIn("鮫洲 11/9 午前", text)
        self.assertIn("予約状況確認/キャンセルで確認してください。自動予約を止めました", text)

    def test_unknown_code_on_new_slot_is_unknown_outcome(self):
        self.switch_queues({"status": "NG", "code": "B9999"})
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres"])
        self.assertFalse(os.path.exists(self.resv))
        self.assertEqual(self.stopped_json()["code"], "UNKNOWN")

    def test_504_with_json_body_on_new_slot_is_unknown_outcome(self):
        # 本物の post_json を通す: 504 で本文が {"message": ...} のとき（コードなし）も結果不明
        def http_error(code, body):
            return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))

        seq = [http_error(504, {"message": "Endpoint request timed out"})]

        def fake_urlopen(req, timeout=0):
            self.calls.append((req.full_url, json.loads(req.data)))
            r = seq.pop(0) if req.full_url == check.PUT_API else None
            if r is not None:
                raise r
            raise AssertionError("unexpected")

        real = REAL_POST_JSON

        def routed(url, payload):
            if url == check.PUT_API:
                with mock.patch.object(check.urllib.request, "urlopen", fake_urlopen):
                    return real(url, payload)
            return self.fake_post(url, payload)

        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        with mock.patch.object(check, "post_json", routed):
            self.run_quiet({"a": vac("20261109")})
        puts = [u for u, _ in self.calls if u == check.PUT_API]
        self.assertEqual(len(puts), 1)  # 元の枠を取り直さない
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertIn("新しい枠の予約結果が不明です", "".join(self.notices[0][1]))

    def test_new_fails_b4002_is_persistently_skipped(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, ok("RES-BACK-555", "REC-BACK-666"))
        self.run_quiet(vs(vac("20261109")))
        n = len(self.calls)
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres", "putres"])
        self.assertFalse(os.path.exists(self.stopped))
        self.assertEqual(self.skip_json(), ["280-20261109-0800"])
        self.run_quiet(vs(vac("20261109")))  # 同じ枠は何度でも狙わない
        self.run_quiet(vs(vac("20261109")))
        self.assertEqual(len(self.calls), n)
        # 別の枠は対象になる
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet(vs(vac("20261109"), vac("20261110")))
        self.assertEqual(self.calls[-1][1]["date"], "20261110")

    def test_skip_pruned_when_slot_disappears_then_reappears(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, ok("RES-BACK-555", "REC-BACK-666"))
        self.run_quiet(vs(vac("20261109")))
        path = os.path.join(self.tmp.name, "switch_skip.json")
        # 空きが残っているあいだは記録を保つ（内容が変わらなければ書き直さない）
        mtime = os.stat(path).st_mtime_ns
        check.prune_switch_skip(vs(vac("20261109"), vac("20261110")))
        self.assertEqual(self.skip_json(), ["280-20261109-0800"])
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)
        # 枠が消えたら外す
        check.prune_switch_skip(vs(vac("20261110")))
        self.assertEqual(self.skip_json(), [])
        # また空いたら対象になる
        n = len(self.calls)
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet(vs(vac("20261109")))
        self.assertGreater(len(self.calls), n)

    def test_rebook_exception_unknown_outcome(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, OSError("timeout"))
        self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        title, lines, prio = self.notices[0]
        self.assertEqual(prio, 5)
        text = "".join(lines)
        self.assertIn("元の枠の取り直し結果が不明です", text)
        self.assertIn("予約状況確認/キャンセルで確認してください。自動予約を止めました", text)
        self.assertNotIn("今は予約がありません", text)

    def test_http5xx_is_unknown_outcome(self):
        # 乗り換え先が 5xx: 元の枠は取り直さない
        self.switch_queues({"code": "HTTP503"})
        self.run_quiet(vs(vac("20261109")))
        self.assertEqual(self.urls(), ["customerops", "cancel", "putres"])
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertIn("結果が不明", "".join(self.notices[0][1]))

    def test_http5xx_on_rebook_is_unknown_outcome(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, {"code": "HTTP502"})
        self.run_quiet(vs(vac("20261109")))
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertNotIn("今は予約がありません", "".join(self.notices[0][1]))

    def test_ok_without_res_no_in_switch_is_save_failure(self):
        self.switch_queues({"status": "OK", "res_no": "", "rec_no": "REC-NEW-444"})
        self.run_quiet(vs(vac("20261109")))
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertEqual(self.notices[0][2], 5)
        self.assertIn("REC-NEW-444", "".join(self.notices[0][1]))
        self.assertIn("保存に失敗したため自動予約を止めました", "".join(self.notices[0][1]))

    def test_new_fails_b4004_rebooks_and_continues(self):
        # B4001/B4004 も確定した失敗: 元の枠を取り直し、その枠は記録して飛ばす（止めない）
        self.switch_queues({"status": "NG", "code": "B4004"}, ok("RES-BACK-555", "REC-BACK-666"))
        self.run_quiet(vs(vac("20261109")))
        d = self.read_reservation()
        self.assertEqual((d["date"], d["res_no"]), ("20261120", "RES-BACK-555"))
        self.assertFalse(os.path.exists(self.stopped))
        self.assertEqual(self.skip_json(), ["280-20261109-0800"])

    def test_cancel_exception_says_unknown(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, OSError("timeout"))
        self.run_quiet({"a": vac("20261109")})
        text = "".join(self.notices[0][1])
        self.assertIn("取り消しの結果が不明です。予約状況確認/キャンセルで確認してください", text)
        self.assertNotIn("そのまま残っています", text)
        self.assertEqual(self.read_reservation()["res_no"], "RES-OLD-111")

    def test_cancel_http_error_says_unknown(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "HTTP404", "_http": 404})
        self.run_quiet({"a": vac("20261109")})
        self.assertIn("取り消しの結果が不明です", "".join(self.notices[0][1]))

    def test_cancel_definite_code_says_still_held(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "B1234"})
        self.run_quiet({"a": vac("20261109")})
        text = "".join(self.notices[0][1])
        self.assertIn("今の予約はそのまま残っています", text)
        self.assertNotIn("取り消しの結果が不明です", text)

    def test_switch_target_lead_time(self):
        self.run_quiet({"a": vac("20261105"), "b": vac("20261107")})
        self.assertEqual(self.calls, [])
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261105"), "b": vac("20261108", "1300", "1400")})
        self.assertEqual(self.calls[-1][1]["date"], "20261108")

    def test_save_failure_after_switch_stops(self):
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        with mock.patch.object(check, "save_reservation", lambda *a: False):
            self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        title, lines, prio = self.notices[0]
        self.assertEqual(prio, 5)
        self.assertIn("RES-NEW-333", "".join(lines))
        self.assertIn("REC-NEW-444", "".join(lines))
        self.assertIn("保存に失敗したため自動予約を止めました", "".join(lines))
        self.assertEqual(len(self.notices), 1)

    def test_save_failure_after_rebook_stops(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, ok("RES-BACK-555", "REC-BACK-666"))
        with mock.patch.object(check, "save_reservation", lambda *a: False):
            self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertIn("RES-BACK-555", "".join(self.notices[0][1]))

    def test_marker_written_before_cancel_and_removed_after(self):
        seen = {}
        orig = self.fake_post

        def spy(url, payload):
            if url == check.CANCEL_API:
                seen["marker"] = os.path.exists(self.marker)
                with open(self.marker, encoding="utf-8") as f:
                    seen["text"] = f.read()
            return orig(url, payload)

        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        with mock.patch.object(check, "post_json", spy):
            self.run_quiet({"a": vac("20261109")})
        self.assertTrue(seen["marker"])
        for s in SECRETS:
            self.assertNotIn(s, seen["text"])
        self.assertFalse(os.path.exists(self.marker))

    def test_marker_remains_if_process_dies_then_stops_next_start(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_quiet({"a": vac("20261109")})
        self.assertTrue(os.path.exists(self.marker))
        n = len(self.calls)
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self.stopped_json()["code"], "INTERRUPTED")
        self.assertIn("乗り換えの途中で中断された可能性があります", "".join(self.notices[-1][1]))

    def test_marker_removed_when_cancel_fails(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A9998"})
        self.run_quiet({"a": vac("20261109")})
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(os.path.exists(self.stopped))

    def test_state_written_before_notify(self):
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261109")})
        self.assertEqual(self.snaps[-1][:2], (True, False))
        self.notices.clear()
        self.snaps.clear()
        self.put_reservation_file("20261120")
        self.switch_queues({"status": "NG", "code": "B4002"}, {"status": "NG", "code": "B9999"})
        self.run_quiet({"a": vac("20261108")})
        self.assertEqual(self.snaps[-1][:2], (False, True))  # 予約削除と停止ファイルが先

    def test_h_reservation_date_today_or_past_no_switch(self):
        # 7日ルールの影響を外して（過去の枠も対象にして）、「予約日が今日以前なら乗り換えない」だけを確かめる
        with mock.patch.object(check, "MIN_LEAD_DAYS", -5):
            for d, earlier in ((TODAY, "20261031"), ("20261031", "20261030")):
                self.put_reservation_file(d)
                self.run_quiet({"a": vac(earlier)})
                self.assertEqual(self.calls, [])
            self.put_reservation_file("20261102")  # 明日の予約なら乗り換える（対照）
            self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
            self.run_quiet({"a": vac("20261101")})
            self.assertEqual(self.urls(), ["customerops", "cancel", "putres"])

    def test_one_switch_per_check(self):
        self.queue(check.CUSTOMER_API, {"code": "A1201"})
        self.queue(check.CANCEL_API, {"code": "A1301"})
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        self.run_quiet({"a": vac("20261109"), "b": vac("20261110")})
        self.assertEqual(self.urls().count("cancel"), 1)

    def test_check_once_prunes_skip_list(self):
        self.switch_queues({"status": "NG", "code": "B4002"}, ok("RES-BACK-555", "REC-BACK-666"))
        far = {"date": "20261125", "starttime": "0800", "endtime": "0900", "capacity": "5", "reservation": "1"}
        target = dict(far, date="20261109")
        data = {"slots": [target]}

        def fetch(month, place):
            return data["slots"] if month == "202611" else []

        def run_check():
            with mock.patch.object(check, "fetch", fetch), mock.patch.object(check, "PLACES", ["280"]), \
                    mock.patch.object(check, "DATE_FROM", "20261106"), mock.patch.object(check, "DATE_TO", "20261127"), \
                    contextlib.redirect_stdout(io.StringIO()):
                check.check_once()

        run_check()
        self.assertEqual(self.skip_json(), ["280-20261109-0800"])
        n = len(self.calls)
        run_check()  # まだ空いている間は狙わない
        self.assertEqual(len(self.calls), n)
        data["slots"] = [far]  # 枠が消える（残りの枠は予約日より後なので何もしない）
        run_check()
        self.assertEqual(self.skip_json(), [])
        data["slots"] = [target]  # また空いたら対象になる
        self.switch_queues(ok("RES-NEW-333", "REC-NEW-444"))
        run_check()
        self.assertGreater(len(self.calls), n)
        self.assertEqual(self.read_reservation()["date"], "20261109")

    def test_skip_list_load_tolerates_bad_content(self):
        path = os.path.join(self.tmp.name, "switch_skip.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(check.load_switch_skip(), [])
        os.remove(path)
        os.mkdir(path)  # 読めない（OSError）
        self.assertEqual(check.load_switch_skip(), [])


class TestMisc(Base):
    def test_openssl_roundtrip_unicode(self):
        text = json.dumps({"slot": "鮫洲 11/10 午前", "x": "あ" * 500}, ensure_ascii=False)
        enc = check.encrypt_text(text)
        self.assertNotIn("鮫洲", enc)
        self.assertEqual(check.decrypt_text(enc), text)

    def test_key_not_on_command_line(self):
        with mock.patch.object(check.subprocess, "run", wraps=check.subprocess.run) as run:
            check.encrypt_text("x")
        cmd = run.call_args[0][0]
        self.assertNotIn(check.STATE_KEY, " ".join(cmd))
        self.assertIn("env:STATE_KEY", cmd)

    def test_post_json_keeps_http_status(self):
        def err(code, body):
            return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))

        for e, want_code, want_http in [
            (err(504, b'{"message":"Endpoint request timed out"}'), "HTTP504", 504),
            (err(400, b'{"code":"B4002"}'), "B4002", 400),
            (err(502, b"<html>"), "HTTP502", 502),
        ]:
            with mock.patch.object(check.urllib.request, "urlopen", side_effect=e):
                r = REAL_POST_JSON("http://x", {})
            self.assertEqual((r["code"], r["_http"]), (want_code, want_http))

    def test_put_reservation_5xx_ignores_body_code(self):
        with mock.patch.object(check, "post_json", lambda u, p: {"code": "B4002", "_http": 503}):
            self.assertEqual(check.put_reservation(vac("20261108")), "HTTP503")

    def test_notify_failure_does_not_skip_booking(self):
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        slot = {"date": "20261108", "starttime": "0800", "endtime": "0900", "capacity": "5", "reservation": "1"}
        out = io.StringIO()
        with mock.patch.object(check, "fetch", lambda m, p: [slot] if m == "202611" else []), \
                mock.patch.object(check, "PLACES", ["280"]), mock.patch.object(check, "DATE_FROM", "20261106"), \
                mock.patch.object(check, "DATE_TO", "20261127"), mock.patch.object(check, "NTFY_TOPIC", "t"), \
                mock.patch.object(check, "notify", REAL_NOTIFY), \
                mock.patch.object(check.urllib.request, "urlopen", side_effect=OSError("down")), \
                contextlib.redirect_stdout(out):
            check.check_once()
        self.assertEqual(self.read_reservation()["date"], "20261108")
        self.assertIn("通知の送信に失敗", out.getvalue())

    def test_new_booking_exception_stops(self):
        self.queue(check.PUT_API, OSError("timeout"))
        self.run_quiet(vs(vac("20261108")))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertFalse(os.path.exists(self.resv))
        title, lines, prio = self.notices[0]
        self.assertEqual(prio, 5)
        self.assertIn("新規予約の結果が不明です。予約状況確認/キャンセルで確認してください。自動予約を止めました", "".join(lines))

    def test_new_booking_http5xx_stops(self):
        self.queue(check.PUT_API, {"code": "HTTP500"})
        self.run_quiet(vs(vac("20261108")))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertEqual(self.notices[0][2], 5)

    def test_new_booking_ok_without_res_no_is_save_failure(self):
        self.queue(check.PUT_API, {"status": "OK", "rec_no": "REC-NEW-444"})
        self.run_quiet(vs(vac("20261108")))
        self.assertFalse(os.path.exists(self.resv))
        self.assertTrue(os.path.exists(self.stopped))
        self.assertIn("REC-NEW-444", "".join(self.notices[0][1]))

    def test_main_returns_immediately_after_window(self):
        with mock.patch.object(check, "DATE_TO", "20261031"), mock.patch.object(check, "LOOP_MINUTES", 5), \
                mock.patch.object(check, "check_once", side_effect=AssertionError("called")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                check.main()
        self.assertIn("監視期間", out.getvalue())

    def test_check_once_end_to_end_no_leak(self):
        self.queue(check.PUT_API, ok("RES-NEW-333", "REC-NEW-444"))
        slot = {"date": "20261108", "starttime": "0800", "endtime": "0900", "capacity": "5", "reservation": "1"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(check, "fetch", lambda m, p: [slot] if m == "202611" else []), \
                mock.patch.object(check, "PLACES", ["280"]), mock.patch.object(check, "DATE_FROM", "20261106"), \
                mock.patch.object(check, "DATE_TO", "20261127"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            check.check_once()
        for s in SECRETS:
            self.assertNotIn(s, out.getvalue() + err.getvalue())
        self.assertEqual(self.read_reservation()["date"], "20261108")


if __name__ == "__main__":
    unittest.main()
