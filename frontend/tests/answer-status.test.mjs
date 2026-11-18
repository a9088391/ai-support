import assert from "node:assert/strict";
import { test } from "node:test";
import * as contracts from "../app/workbench/contracts.ts";

// 資料不足須直接說明知識缺口；技術失敗不能再共用含糊的未完成標題。
test("資料不足與各種技術失敗顯示不同且具體的狀態標題", () => {
  // 先核對使用者要求的不足標題，避免被誤當成系統故障。
  const titles = contracts.answerStatusTitles;
  assert.equal(titles?.insufficient_evidence, "知識資料不足");
  assert.match(
    contracts.answerStatusMessages.insufficient_evidence,
    /知識資料/,
  );

  // 技術狀態保留可採取行動的原因，且不得合併為同一標題。
  assert.match(titles.invalid_output, /驗證/);
  assert.match(titles.rate_limited, /額度.*速率/);
  assert.match(titles.timed_out, /逾時/);
  assert.match(titles.unavailable, /服務.*無法使用/);
  const failureTitles = [
    titles.insufficient_evidence,
    titles.invalid_output,
    titles.rate_limited,
    titles.timed_out,
    titles.unavailable,
  ];
  assert.equal(new Set(failureTitles).size, failureTitles.length);
  assert(!failureTitles.includes("問答未完成"));

  // 未確認的結果與已停止等待仍各自說明，不改成可以自動重送的失敗。
  assert.equal(titles.unconfirmed, "回答結果待確認");
  assert.equal(titles.stopped, "回答未完成");
});

// 呈現用的unconfirmed不擴大後端DTO狀態，既有失敗終態仍保留搜尋證據。
test("新增狀態標題不放寬問答終態的公開契約", () => {
  // 用同一份合法DTO核對每種既有失敗，來源與空回答契約保持一致。
  const output = {
    conversation_id: "de587130-ca91-4c2f-976b-6cf2363274f5",
    question_id: "3f712cef-334a-45b2-aac3-e44b0bf06983",
    attempt_id: "40c744e0-2a35-4e81-97c0-b0c05ebdaa0c",
    model: contracts.SETUP_MODEL,
    answer: null,
    citations: [],
    search_results: [
      {
        document_id: "taiwan",
        title: "臺灣",
        version: "test-v1",
        fragment_id: "geography",
        excerpt: "已取得的搜尋證據，不代表足以回答。",
      },
    ],
  };
  for (const status of [
    "insufficient_evidence",
    "invalid_output",
    "rate_limited",
    "timed_out",
    "unavailable",
  ]) {
    const parsed = contracts.readAnswerOutput({ ...output, status });
    assert.equal(parsed.status, status);
    assert.deepEqual(parsed.search_results, output.search_results);
    assert.equal(parsed.answer, null);
    assert.deepEqual(parsed.citations, []);
  }

  // 純前端待確認、停止與未知值都不得冒充後端終態。
  for (const status of ["unconfirmed", "stopped", "unexpected-status"]) {
    assert.equal(contracts.readAnswerOutput({ ...output, status }), null);
  }
});
