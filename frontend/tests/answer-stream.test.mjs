import assert from "node:assert/strict";
import { test } from "node:test";
import { readAnswerStream } from "../app/workbench/answer-stream.ts";

const identity = {
  type: "accepted",
  conversation_id: "69f345d1-982a-4045-879c-49f00b41ee3f",
  question_id: "9504d3d1-cd5f-454c-826e-813e36bcd414",
  attempt_id: "265c090b-8be2-4bd9-9bba-fca6774b83c6",
};
const terminal = { type: "result", result: { answer: "繁體😀" } };
const acceptedLine = JSON.stringify(identity) + "\n";
const terminalLine = JSON.stringify(terminal) + "\n";
const encoder = new TextEncoder();

// 用真實位元組串流控制傳輸分段；不以逐列 JSON 陣列代替傳輸。
function streamBytes(parts) {
  return new ReadableStream({
    // 同步加入指定區塊，避免測試依賴延遲或排程。
    start(controller) {
      for (const part of parts) {
        controller.enqueue(part);
      }
      controller.close();
    },
  });
}

// 跨區塊甚至跨 UTF-8 碼位時，受理及終態仍只各解析一次。
test("逐位元組切開受理與繁中終態", async () => {
  // 每次只送一個位元組，確定同時跨越識別、換行與多位元字元。
  const bytes = encoder.encode(acceptedLine + terminalLine);
  const parts = Array.from(
    bytes,
    // 每個區塊保留原始位元組，不先解碼成單字元。
    (byte) => Uint8Array.of(byte),
  );
  const admissions = [];
  const result = await readAnswerStream(
    streamBytes(parts),
    // 記錄真正採用的事件，核對身分交付早於最後回傳。
    (frame) => {
      admissions.push(frame);
      return true;
    },
  );
  assert.deepEqual(admissions, [identity]);
  assert.deepEqual(result, {
    status: 200,
    data: terminal.result,
    retryAfter: null,
  });
});

// 合併區塊也要分別解析；沒有最後換行就不能採用終態。
test("合併傳輸可讀，缺少終態換行則拒絕", async () => {
  // 此測試只檢查傳輸，固定接受有效的第一筆事件。
  const accept = () => true;
  const stream = streamBytes([encoder.encode(acceptedLine + terminalLine)]);
  assert.deepEqual(
    (await readAnswerStream(stream, accept)).data,
    terminal.result,
  );

  // 移除終態的換行，保留語法正確的 JSON 仍應拒絕。
  const truncated = streamBytes([
    encoder.encode(acceptedLine + terminalLine.slice(0, -1)),
  ]);
  await assert.rejects(readAnswerStream(truncated, accept), /提前中斷/);
});

// 建單先完成時，已受理後的 404 仍交給擁有本次問答的畫面處理。
test("受理後傳回固定 404；未受理先來終態則拒絕", async () => {
  // 串流錯誤保留 HTTP 語意，既有文案路徑會驗證 detail。
  const detail = "找不到這次問答。";
  const failureLine =
    JSON.stringify({ type: "error", status: 404, detail }) + "\n";
  const stream = streamBytes([encoder.encode(acceptedLine + failureLine)]);
  // 本案例不處理操作成員關係；只驗證框架和狀態順序。
  const accept = () => true;
  assert.deepEqual(await readAnswerStream(stream, accept), {
    status: 404,
    data: { detail },
    retryAfter: null,
  });
  await assert.rejects(
    readAnswerStream(streamBytes([encoder.encode(terminalLine)]), accept),
    /無法確認/,
  );
});

// 同一資料流尚未完成時就要交付受理，不能等模型終態或 EOF 才開放建單。
test("終態尚未送出時可先使用受理識別", async () => {
  const admission = Promise.withResolvers();
  let settled = false;
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const response = readAnswerStream(
    readable,
    // 記錄受理時點；下一個寫入只在受理完成後才會發生。
    (frame) => {
      admission.resolve(frame);
      return true;
    },
  ).then(
    // 驗證終態與受理的兩個時點沒有混在一起。
    (result) => {
      settled = true;
      return result;
    },
  );

  // 在觀察到受理之前保持終態未送出。
  await writer.write(encoder.encode(acceptedLine));
  assert.deepEqual(await admission.promise, identity);
  assert.equal(settled, false);
  await writer.write(encoder.encode(terminalLine));
  assert.deepEqual((await response).data, terminal.result);
});
