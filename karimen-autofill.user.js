// ==UserScript==
// @name         仮免許予約 自動入力
// @description  「仮免許学科試験」選択 → 予約者情報入力 → 受験場所選択 までを自動で進め、カレンダーを表示する
// @match        https://provisional.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/01/html/main.html*
// @grant        GM.getValue
// @grant        GM.setValue
// @run-at       document-end
// ==/UserScript==

// 予約者情報はこのスクリプトの保存領域（スマホ内）にだけ保存する。
// 初回は画面に入力欄が出る。あとから変更するときは右下の「自動入力の設定」を押す。

(async function () {
  const KEY = "me";
  const pad = (v) => String(v).padStart(2, "0");
  const q = (s) => document.querySelector(s);
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const fire = (el, type) => el.dispatchEvent(new Event(type, { bubbles: true }));
  const setVal = (sel, v) => {
    const el = q(sel);
    el.value = v;
    fire(el, "change");
    fire(el, "blur");
  };

  function openSettings(me) {
    // サイトのCSSの影響を受けないよう Shadow DOM の中に作る
    const box = document.createElement("div");
    const root = box.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        .bg { position:fixed; inset:0; z-index:99999; background:rgba(0,0,0,.5);
              display:flex; align-items:center; justify-content:center; font:16px/1.6 sans-serif; color:#000; }
        form { background:#fff; padding:20px; border-radius:12px; width:min(90vw,360px); box-sizing:border-box; }
        label { display:block; margin-top:10px; font-size:14px; }
        input, select { width:100%; box-sizing:border-box; font-size:16px; padding:6px; margin-top:2px; }
        .row { display:flex; gap:6px; }
        button { width:100%; margin-top:16px; padding:10px; font-size:16px; }
      </style>
      <div class="bg"><form>
        <b>自動入力の設定</b>
        <label>氏名（カナ又はアルファベット）<input name="name" placeholder="メンキョハナコ" required></label>
        <label>生年月日（年 / 月 / 日）</label>
        <div class="row">
          <input name="birthYear" inputmode="numeric" placeholder="1986" required>
          <input name="birthMonth" inputmode="numeric" placeholder="8" required>
          <input name="birthDay" inputmode="numeric" placeholder="1" required>
        </div>
        <label>電話番号（ハイフンなし）<input name="tel" inputmode="tel" placeholder="09011112222" required></label>
        <label>受験場所<select name="place"><option value="280">鮫洲</option><option value="270">府中</option></select></label>
        <button type="submit">保存して進む</button>
      </form></div>`;
    const form = root.querySelector("form");
    for (const [k, v] of Object.entries(me || {})) if (form[k]) form[k].value = v;
    document.body.appendChild(box);
    return new Promise((resolve) => {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(form));
        await GM.setValue(KEY, JSON.stringify(data));
        box.remove();
        resolve(data);
      });
    });
  }

  let me = JSON.parse((await GM.getValue(KEY, "null")) || "null");

  const btn = document.createElement("button");
  btn.textContent = "自動入力の設定";
  btn.type = "button";
  btn.style.cssText = "position:fixed;right:8px;bottom:8px;z-index:99998;font-size:12px;opacity:.7";
  btn.onclick = async () => { me = await openSettings(me); };
  document.body.appendChild(btn);

  if (!me) me = await openSettings(null);

  const done = {};
  const startedAt = Date.now();
  const timer = setInterval(() => {
    if (Date.now() - startedAt > 60000) return clearInterval(timer);

    // 1. 「仮免許学科試験」を選ぶ
    if (!done.type && visible(q("#page01"))) {
      done.type = true;
      q('input[name=typeChoice][value="1"]').click();
      return;
    }

    // 2. 予約者情報を入力して「進む」
    if (!done.info && visible(q("#page03"))) {
      done.info = true;
      setVal("#strNameKana", me.name);
      setVal("#birthYear", me.birthYear);
      setVal("#birthMonth", pad(me.birthMonth));
      setVal("#birthDay", pad(me.birthDay));
      setVal("#strTelNumber", me.tel);
      setTimeout(() => q("#nextPageBtn03").click(), 500);
      return;
    }

    // 3. 受験場所を選ぶ → カレンダーが表示されたら終了
    const place = q(`input[name=placeChoice][value^="${me.place || "280"}:"]`);
    if (!done.place && visible(q("#page04")) && place) {
      done.place = true;
      place.click();
      clearInterval(timer);
    }
  }, 300);
})();
