export const NDJSON = "application/x-ndjson";
// 允許完整的 40 筆搜尋片段及引用，同時限制異常的未換行回應。
const MAX_FRAME_LENGTH = 4 * 1024 * 1024;

export interface ApiResult {
  readonly status: number | null;
  readonly data: unknown;
  readonly retryAfter: string | null;
}

// 以換行確認一列完整後才解析，保留跨網路區塊的 UTF-8 與 JSON。
async function* readFrames(body: ReadableStream<Uint8Array>) {
  // 嚴格解碼，不讓壞掉的 UTF-8 進入任何識別或證據。
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let pending = "";

  try {
    // 一次讀取可能包含多個事件，也可能只是一個碼位的一部分。
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      pending += decoder.decode(chunk.value, { stream: true });

      // 遇到終態前只解析完整行，未完成的尾端留待下一次讀取。
      let newline = pending.indexOf("\n");
      while (newline !== -1) {
        if (newline > MAX_FRAME_LENGTH) {
          throw new Error("問答事件超過大小限制。");
        }
        const frame: unknown = JSON.parse(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
        yield frame;
        newline = pending.indexOf("\n");
      }
      if (pending.length > MAX_FRAME_LENGTH) {
        throw new Error("問答事件超過大小限制。");
      }
    }

    // EOF 不代表下一筆已完成；不允許解析缺少換行的最後一筆。
    if (pending + decoder.decode()) {
      throw new Error("問答串流提前中斷。");
    }
  } finally {
    // 完成、錯誤或上層拒絕後都停止讀取，且不延長傳輸期限。
    void reader.cancel().catch(
      // 中止或讀取失敗後，串流可能已不能取消。
      () => {},
    );
    reader.releaseLock();
  }
}

// 只接受「受理後接一筆終態」；由發起的工作台核對受理識別。
export async function readAnswerStream(
  body: ReadableStream<Uint8Array>,
  accept: (frame: Record<string, unknown>) => boolean,
): Promise<ApiResult> {
  let accepted = false;

  // 回應順序有意義；終態絕不能先於本次操作的受理。
  for await (const value of readFrames(body)) {
    if (typeof value !== "object" || value === null) {
      break;
    }
    const frame = value as Record<string, unknown>;
    if (!accepted) {
      if (frame.type !== "accepted" || !accept(frame)) {
        break;
      }
      accepted = true;
      continue;
    }

    // 將終態錯誤交由既有固定文案處理，不直接呈現服務端本文。
    if (frame.type === "result") {
      return { status: 200, data: frame.result, retryAfter: null };
    }
    // prettier-ignore
    if (
      frame.type === "error"
      && typeof frame.status === "number"
      && Number.isInteger(frame.status)
      && frame.status >= 400
      && frame.status < 600
      && typeof frame.detail === "string"
    ) {
      return {
        status: frame.status,
        data: { detail: frame.detail },
        retryAfter: null,
      };
    }
    break;
  }

  // 無終態的已受理操作由呼叫端以安全讀回處理。
  throw new Error("無法確認問答串流結果。");
}
