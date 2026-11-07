// 僅供本機瀏覽器驗證；固定綁定 loopback 並只提供啟用狀態測試資料。
import { createServer } from "node:http";
import { readFile, realpath } from "node:fs/promises";
import {
  dirname,
  extname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

const HOST = "127.0.0.1";
const DEFAULT_PORT = 8021;
const MIN_PORT = 1024;
const MAX_PORT = 65535;
const MODEL = "gemini-3.5-flash-lite";
const fixtureModes = [
  "unconfigured",
  "unverified",
  "ready",
  "invalid",
  "unavailable",
  "malformed",
  "timeout",
];
const messages = {
  unconfigured: "尚未設定 Gemini API Key。請由本機操作人員完成設定。",
  unverified: "目前的 Gemini 設定尚未通過驗證。請由本機操作人員執行驗證。",
  ready: "目前的 Gemini 設定已通過驗證。",
  invalid: "Google 已拒絕目前的憑證。請由本機操作人員更換 Key 並重新驗證。",
};
const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};
const args = process.argv.slice(2);
const mode = args[0];

// 埠號只接受十進位安全整數，預設值保留供一般本機測試使用。
function parsePort(value) {
  // 未提供埠時沿用預設埠，維持原有測試啟動方式。
  if (value === undefined) {
    return DEFAULT_PORT;
  }

  // 拒絕小數、科學記號及其他非十進位輸入，避免模糊轉換。
  if (!/^\d+$/.test(value)) {
    return null;
  }

  // 轉換後再確認為安全整數，避免過大值失去精度。
  const port = Number(value);
  if (!Number.isSafeInteger(port)) {
    return null;
  }

  // 只允許一般非特權埠範圍，避免監聽特殊或保留值。
  if (port < MIN_PORT || port > MAX_PORT) {
    return null;
  }

  return port;
}

const port = parsePort(args[1]);

// 只允許安全模式、必要參數及最多一個有效本機埠號。
// prettier-ignore
if (
  args.length < 1
  || args.length > 2
  || !fixtureModes.includes(mode)
  || port === null
) {
  console.error(
    `用法：node tests/fixture-server.mjs <${fixtureModes.join("|")}> [port: ${MIN_PORT}-${MAX_PORT}]`,
  );
  process.exit(2);
}

// 解析靜態輸出根目錄，缺少建置結果時明確要求先執行 generate。
const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
let publicRoot;
try {
  publicRoot = await realpath(resolve(frontendRoot, ".output/public"));
} catch {
  console.error("找不到 .output/public，請先執行 npm run generate。");
  process.exit(1);
}

// 回傳固定 JSON 及禁止快取標頭，讓狀態案例不被瀏覽器沿用。
function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(JSON.stringify(payload));
}

// 只提供前端檔案及核准的狀態 GET，避免測試器變成一般 API 替身。
async function handleRequest(request, response) {
  // 只接受本機瀏覽器所需的 GET，其他方法不會觸及測試狀態。
  if (request.method !== "GET") {
    response.writeHead(405, { Allow: "GET" });
    response.end();
    return;
  }

  // 使用固定基底解析路徑，不採信請求提供的 Host 作為目的位址。
  let requestUrl;
  try {
    requestUrl = new URL(request.url ?? "/", `http://${HOST}:${port}`);
  } catch {
    response.writeHead(400);
    response.end();
    return;
  }

  // 測試 API 只有狀態 GET；錯誤、格式錯誤及逾時都只存在於此測試程序。
  if (requestUrl.pathname === "/api/setup") {
    console.log(`TEST FIXTURE GET /api/setup (${mode})`);

    // 503 模式使用固定本文，驗證前端不把 HTTP 錯誤當成有效 DTO。
    if (mode === "unavailable") {
      sendJson(response, 503, { detail: "測試用服務暫時無法確認狀態。" });
      return;
    }

    // ready 外觀搭配錯誤模型，驗證固定模型檢查會關閉未預期回應。
    if (mode === "malformed") {
      sendJson(response, 200, {
        status: "ready",
        message: "測試資料的模型不符合固定契約。",
        model: "unexpected-test-model",
      });
      return;
    }

    // 延後回應超過前端界線，驗證逾時後不接受遲到的成功狀態。
    if (mode === "timeout") {
      // 回應晚於前端逾時界線，驗證錯誤狀態可恢復手動重試。
      setTimeout(() => {
        // 用戶端逾時後已中止連線時，不再嘗試送出遲到回應。
        if (!response.destroyed) {
          sendJson(response, 200, {
            status: "unconfigured",
            message: messages.unconfigured,
            model: MODEL,
          });
        }
      }, 11_000);
      return;
    }

    sendJson(response, 200, {
      status: mode,
      message: messages[mode],
      model: MODEL,
    });
    return;
  }

  // ready 模式只模擬未登入讀取，絕不建立 Cookie 或提供示範密碼。
  if (requestUrl.pathname === "/api/session" && mode === "ready") {
    console.log("TEST FIXTURE GET /api/session (unauthenticated)");
    sendJson(response, 401, { detail: "測試替身不保存登入狀態。" });
    return;
  }

  // 不替其他 API 路由提供資料，避免把測試替身誤用成業務服務。
  if (requestUrl.pathname.startsWith("/api/")) {
    sendJson(response, 404, { detail: "測試替身只提供啟用狀態。" });
    return;
  }

  // 解碼及正規化靜態路徑後，僅允許讀取輸出目錄內的實際檔案。
  let requestedPath;
  try {
    requestedPath = decodeURIComponent(requestUrl.pathname);
  } catch {
    response.writeHead(400);
    response.end();
    return;
  }

  const requestedFile =
    requestedPath === "/" ? "index.html" : requestedPath.slice(1);
  const candidate = resolve(publicRoot, requestedFile);
  const candidateRelative = relative(publicRoot, candidate);
  // prettier-ignore
  if (
    candidateRelative === ".."
    || candidateRelative.startsWith(`..${sep}`)
    || isAbsolute(candidateRelative)
  ) {
    response.writeHead(404);
    response.end();
    return;
  }

  // 再檢查實際路徑，避免輸出目錄內的連結指向其外部位置。
  let filePath;
  try {
    filePath = await realpath(candidate);
  } catch {
    response.writeHead(404);
    response.end();
    return;
  }

  const actualRelative = relative(publicRoot, filePath);
  // prettier-ignore
  if (
    actualRelative === ".."
    || actualRelative.startsWith(`..${sep}`)
    || isAbsolute(actualRelative)
  ) {
    response.writeHead(404);
    response.end();
    return;
  }

  // 將輸出檔案原樣送給瀏覽器，不產生或寫入專案以外的資料。
  try {
    const content = await readFile(filePath);
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type":
        mimeTypes[extname(filePath)] ?? "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
    });
    response.end(content);
  } catch {
    response.writeHead(404);
    response.end();
  }
}

// 建立僅連接本機輸出的 HTTP 測試服務。
const server = createServer((request, response) => {
  // 將每個請求交給唯讀路由處理器，不保留跨請求狀態。
  void handleRequest(request, response);
});

// 啟動後清楚標示測試模式，避免把 fixture 狀態誤認為真實 Key 驗證。
function reportListening() {
  console.log(`TEST ONLY (${mode}) http://${HOST}:${port}`);
}

// 啟動失敗只回報安全訊息並留下非零狀態，不輸出系統例外細節。
server.on("error", () => {
  // 提示操作人員更換本機埠，不嘗試中止未知監聽器。
  console.error(`無法啟動測試服務 ${HOST}:${port}；請確認埠號可用。`);
  // 設定非零結束碼，避免呼叫端把綁定失敗當成就緒。
  process.exitCode = 1;
});

server.listen(port, HOST, reportListening);
