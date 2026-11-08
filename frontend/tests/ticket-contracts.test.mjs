import assert from "node:assert/strict";
import { test } from "node:test";
import {
  readTicketDetail,
  readTicketPage,
} from "../app/workbench/contracts.ts";

const open = {
  id: "69f345d1-982a-4045-879c-49f00b41ee3f",
  description: "問題描述",
  question: "原始問題",
  created_at: "2026-11-08T00:00:00Z",
  status: "open",
  resolution: null,
  closed_at: null,
};
const closed = {
  ...open,
  status: "closed",
  resolution: "  <b>已處理</b> 😀\n",
  closed_at: "2026-11-08T01:00:00+00:00",
};
const evidence = {
  question_id: "9504d3d1-cd5f-454c-826e-813e36bcd414",
  attempt_id: "265c090b-8be2-4bd9-9bba-fca6774b83c6",
  answer_status: "stopped",
  answer: null,
  citations: [],
  search_results: [],
};

// 同一筆已結案快照可供本人與客服讀取，建單回應仍限本人。
test("客服查閱與建單回應使用各自的建立人範圍", () => {
  // 核對角色例外只開放客服查閱，且不接受未知的建立人。
  const detail = { ...closed, ...evidence, created_by: "user" };
  assert.deepEqual(readTicketDetail(detail, "support", "visible"), detail);
  assert.deepEqual(readTicketDetail(detail, "user"), detail);
  assert.equal(readTicketDetail(detail, "support"), null);
  const supportDetail = { ...detail, created_by: "support" };
  assert.deepEqual(
    readTicketDetail(supportDetail, "support", "visible"),
    supportDetail,
  );
  assert.equal(readTicketDetail(supportDetail, "user", "visible"), null);
  assert.equal(
    readTicketDetail(
      { ...detail, created_by: "unknown" },
      "support",
      "visible",
    ),
    null,
  );
});

// 摘要的結案欄位須一致且保留原文，未知或半套終態不得顯示。
test("工單分頁接受完整開啟與結案狀態", () => {
  // 一頁可混合兩種狀態，保留包含空白、換行與符號的結果。
  const page = { tickets: [open, closed], has_more: false };
  assert.deepEqual(readTicketPage(page, 20), page);

  // 缺少任一結案欄位、時區錯誤或開啟卻含處理結果皆不是合法摘要。
  for (const ticket of [
    { ...open, question: undefined },
    { ...open, question: 42 },
    { ...open, resolution: closed.resolution },
    { ...closed, resolution: null },
    { ...closed, closed_at: null },
    { ...closed, closed_at: "2026-11-08T09:00:00+08:00" },
  ]) {
    assert.equal(readTicketPage({ ...page, tickets: [ticket] }, 20), null);
  }
});

// 清單與詳情都要拒絕無效的已結案結果，避免把錯誤讀回當成寫入成功。
test("已結案結果遵守原文與 4,000 個 Unicode 字元限制", () => {
  // 補充平面字元與換行各算一個碼位，且回應須保留完整原文。
  const resolution = "\n\n" + "😀".repeat(3997) + "\r";
  const ticket = { ...closed, resolution };
  const detail = { ...ticket, ...evidence, created_by: "user" };
  const page = { tickets: [ticket], has_more: false };
  assert.deepEqual(readTicketPage(page, 20), page);
  assert.deepEqual(readTicketDetail(detail, "support", "visible"), detail);

  // 空白、控制字元、未配對代理字元與超限文字都不能成為結案證明。
  for (const invalid of [
    "",
    "\t\n　",
    "結果\u0000",
    "結果\u007f",
    "結果\ud800",
    "結果\udfff",
    resolution + "字",
  ]) {
    assert.equal(
      readTicketPage(
        { ...page, tickets: [{ ...ticket, resolution: invalid }] },
        20,
      ),
      null,
    );
    assert.equal(
      readTicketDetail(
        { ...detail, resolution: invalid },
        "support",
        "visible",
      ),
      null,
    );
  }
});
