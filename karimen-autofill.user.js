// ==UserScript==
// @name         仮免許予約 自動入力
// @description  「仮免許学科試験」選択 → 予約者情報入力 → 受験場所選択 までを自動で進め、カレンダーを表示する
// @match        https://provisional.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/01/html/main.html*
// @inject-into  page
// @run-at       document-end
// ==/UserScript==

(function () {
  // ▼▼▼ ここに自分の情報を入力してください ▼▼▼
  const ME = {
    name: "",        // 氏名（カナ又はアルファベット）例: "メンキョハナコ"
    birthYear: "",   // 例: "1986"
    birthMonth: "",  // 例: "8"
    birthDay: "",    // 例: "1"
    tel: "",         // ハイフンなし 例: "09011112222"
  };
  const PLACE = "280"; // 受験場所 280=鮫洲, 270=府中
  // ▲▲▲ ここまで ▲▲▲

  const pad = (v) => String(v).padStart(2, "0");
  const done = {};
  const startedAt = Date.now();

  const timer = setInterval(() => {
    const $ = window.jQuery;
    if (!$) return;
    if (Date.now() - startedAt > 60000) return clearInterval(timer);

    // 1. 「仮免許学科試験」を選ぶ
    if (!done.type && $("#page01").is(":visible")) {
      done.type = true;
      $('input[name=typeChoice][value="1"]')[0].click();
      return;
    }

    // 2. 予約者情報を入力して「進む」
    if (!done.info && $("#page03").is(":visible") && ME.name) {
      done.info = true;
      $("#strNameKana").val(ME.name).trigger("change").trigger("blur");
      $("#birthYear").val(ME.birthYear).trigger("change");
      $("#birthMonth").val(pad(ME.birthMonth)).trigger("change");
      $("#birthDay").val(pad(ME.birthDay)).trigger("change");
      $("#strTelNumber").val(ME.tel).trigger("change").trigger("blur");
      setTimeout(() => $("#nextPageBtn03").trigger("click"), 500);
      return;
    }

    // 3. 受験場所を選ぶ → カレンダーが表示されたら終了
    const place = $(`input[name=placeChoice][value^="${PLACE}:"]`);
    if (!done.place && $("#page04").is(":visible") && place.length) {
      done.place = true;
      place[0].click();
      clearInterval(timer);
    }
  }, 300);
})();
