import assert from "node:assert/strict";
import { test } from "node:test";
import { revealAnswer } from "../app/workbench/answer-reveal.ts";

// 只控制原生frame介面所需的時間與佇列，不安裝DOM或模擬Vue內部狀態。
function frameClock() {
  let time = 100;
  let nextId = 0;
  const pending = new Map();
  return {
    pending,
    frames: {
      // 起點與frame timestamp共用同一時鐘，避免測試依賴真正等待。
      now() {
        return time;
      },
      // 保存下一個frame，讓測試逐步觀察中間文字。
      request(callback) {
        const id = ++nextId;
        pending.set(id, callback);
        return id;
      },
      // 取消應移除佇列項目；測試仍可保留舊callback驗證晚到防護。
      cancel(id) {
        pending.delete(id);
      },
    },
    // 每次只執行目前一批frame，後續frame仍需下一次推進。
    advance(milliseconds) {
      time += milliseconds;
      const callbacks = [...pending.values()];
      pending.clear();
      for (const callback of callbacks) {
        callback(time);
      }
    },
  };
}

// 中文、ZWJ表情、組合字與旗幟都應以完整字素呈現，不留下半個字元。
test("逐字進度保留Unicode字素且終態只交付完整原文一次", () => {
  // 固定時鐘讓每個字素邊界都能被單獨觀察。
  const clock = frameClock();
  const values = [];
  // 觀察對外呈現值，不以內部游標或切片實作當成測試結果。
  revealAnswer(
    "臺👨‍👩‍👧‍👦e\u0301🇹🇼",
    false,
    (text) => values.push(text),
    clock.frames,
  );
  assert.deepEqual(values, [""]);

  // 四個字素各推進一次，完整答案必須保留原始組合形式。
  for (let i = 0; i < 3; i++) {
    clock.advance(24);
  }
  const terminalFrame = [...clock.pending.values()][0];
  clock.advance(24);
  assert.deepEqual(values, ["", "臺", "臺👨‍👩‍👧‍👦", "臺👨‍👩‍👧‍👦e\u0301", "臺👨‍👩‍👧‍👦e\u0301🇹🇼"]);
  assert.equal(clock.pending.size, 0);

  // 即使已完成的callback晚到，也不能再次覆寫或交付終態。
  terminalFrame(9999);
  assert.equal(values.length, 5);
});

// 長答案的動畫有固定短上限，不能隨正文長度無限等待。
test("長答案在一點二秒完成且逐字前綴不取代原文", () => {
  // 長文仍使用同一可控時鐘，避免真正等待一點二秒。
  const clock = frameClock();
  const text = "知識😀".repeat(2000);
  const values = [];
  // 同時保留完整輸入，核對動畫只影響可見值。
  revealAnswer(text, false, (value) => values.push(value), clock.frames);
  clock.advance(1199);
  assert(values.at(-1).length > 0);
  assert.notEqual(values.at(-1), text);
  clock.advance(1);
  assert.equal(values.at(-1), text);
  assert.equal(clock.pending.size, 0);
});

// 減少動態效果及空字串都不建立frame，直接提供最終結果。
test("reduced motion與空字串立即全文且不排程", () => {
  // 逐一確認兩種立即呈現輸入，不建立任何排程。
  for (const [text, reducedMotion] of [
    ["繁體👩‍💻", true],
    ["", false],
  ]) {
    const clock = frameClock();
    const values = [];
    // 立即路徑也使用同一個呈現入口，避免多送一次空字串。
    const cancel = revealAnswer(
      text,
      reducedMotion,
      (value) => values.push(value),
      clock.frames,
    );
    assert.deepEqual(values, [text]);
    assert.equal(clock.pending.size, 0);
    cancel();
    assert.deepEqual(values, [text]);
  }
});

// 換答案與卸載共用取消契約，舊frame晚到不得覆寫新文字。
test("取消與替換答案後的舊frame不能再更新畫面", () => {
  // 兩份答案使用同一畫面出口，讓舊值覆寫可直接被發現。
  const clock = frameClock();
  const values = [];
  // 用同一個畫面出口重現舊答案取消後立即開始新答案。
  const render = (value) => values.push(value);
  const cancelOld = revealAnswer("舊答案", false, render, clock.frames);
  clock.advance(24);
  const staleOld = [...clock.pending.values()][0];
  cancelOld();
  assert.equal(clock.pending.size, 0);
  const cancelNew = revealAnswer("新答案", false, render, clock.frames);
  staleOld(9999);
  assert.equal(values.at(-1), "");
  clock.advance(24);
  assert.equal(values.at(-1), "新");

  // 卸載後即使取消未能阻止已取出的callback，也不可再寫入畫面。
  const staleNew = [...clock.pending.values()][0];
  cancelNew();
  const settled = [...values];
  staleNew(9999);
  assert.deepEqual(values, settled);
  assert.equal(clock.pending.size, 0);
});
