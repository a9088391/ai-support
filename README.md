# AI 客服工單與知識查詢

目前已實作本機啟動、Gemini 設定與啟用門檻；登入、知識問答及工單功能尚在後續開發範圍。FastAPI 同源提供 Nuxt 靜態頁面，並使用 PostgreSQL 18 保存啟用狀態與模型用量。

## 啟動與驗證

需安裝支援 `up --wait` 的 Docker Compose，以及 Python 3.10 或以上版本。Docker 映像包含應用程式及前端建置使用的 Python、Node.js，主機只用 Python 執行本機設定命令。以下命令皆從本專案根目錄執行。

```sh
./bin/local up
```

啟動成功後開啟 <http://127.0.0.1:8020/>。沒有設定 Key 也能啟動，畫面會顯示啟用說明。資料庫僅在本案的 Docker 網路內使用。

先在 Google AI Studio 確認專案仍在免費層，然後執行以下命令。設定命令會在終端提示輸入 Key，輸入時不回顯。驗證命令結束後，重新整理畫面或使用畫面上的狀態更新操作。 [Google AI Studio API Key](https://aistudio.google.com/apikey) · [Gemini API 費用與免費層](https://ai.google.dev/gemini-api/docs/billing)

```sh
./bin/local set-key
./bin/local verify-key
```

- 本機設定支援新版 `AQ.` Key 格式；儲存成功不代表已啟用，仍須執行 `verify-key`。格式檢查不會顯示輸入內容。[Google 的 Key 格式轉換說明](https://discuss.ai.google.dev/t/bring-back-aiza-format-api-keys/170016)
- 驗證使用 `gemini-3.5-flash-lite`，每次手動操作最多送出一筆簡短的 Gemini Interactions 請求。成功代表當下可使用該模型。 [Gemini 模型說明](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- 設定更換後需要重新驗證。同一設定的成功紀錄在重啟後保留。限流、逾時或供應商暫時錯誤會保留既有驗證狀態。確認憑證無效後，業務 API 會暫停使用。
- 驗證會計入本案每日 100 次額度，以臺灣時間午夜換日。發送狀態不明時保留用量。Google 配額獨立計算，可能先達上限。 [Gemini API 配額](https://ai.google.dev/gemini-api/docs/rate-limits)
- 本機秘密存放於專案的 `.local/` 私有目錄。保留 `.local/database-password` 與 PostgreSQL volume，既有資料庫才能繼續連線。
- 若 Key 檔內容損毀，可重新執行 `set-key` 輸入合法 Key 復原，再執行 `verify-key`。權限、擁有者或符號連結不安全時會停止並保留原檔；不會以復原為由略過檔案安全檢查。
- 免費服務的輸入與輸出可能用於改善產品及人工檢閱。展示請使用虛構、非敏感內容。 [Gemini API 服務條款](https://ai.google.dev/gemini-api/terms)

`./bin/local stop` 停止本案容器並保留資料。再次執行 `./bin/local up` 會沿用資料與本機設定，啟動時不呼叫模型。

## 開發與測試

```sh
./bin/local check
./bin/local test
./bin/local stop-test
```

`check` 執行 Python 語法、Ruff、mypy 與套件相容性檢查。`test` 使用獨立的 `ai-support-test` 專案與 PostgreSQL 18，透過受控 HTTP 傳輸測試官方 Gemini SDK，包含真實資料庫交易與併發行為。測試容器不掛載正式 Key，測試資料庫在停止時丟棄。

前端開發使用 Node.js 24.22.2；Docker 建置已鎖定相同版本。使用符合 `frontend/package.json` 的主機 Node.js 後，可另外執行格式、型別及靜態輸出檢查：

```sh
cd frontend
npm ci --no-audit --no-fund
npm run format:check
npm run typecheck
npm run generate
```

在 `frontend` 目錄執行以下命令，可於 <http://127.0.0.1:18021/> 檢查啟用畫面的狀態分支，完成後按 `Ctrl+C` 停止：

```sh
node tests/fixture-server.mjs invalid 18021
```

模式包括 `unconfigured`、`unverified`、`ready`、`invalid`、`unavailable`（503）、`malformed`（不符合契約）及 `timeout`（超過前端十秒界線）。埠號可省略，預設為 8021；只能綁定本機。此工具只讀取前端建置結果並提供固定狀態資料，不讀取 Key、不連資料庫、不呼叫模型，也不會隨正式應用啟動。測試畫面的 `ready` 不能取代真實 `verify-key` 驗收。

目前已通過 35 項後端測試、Python 靜態檢查、前端格式／型別／建置及桌面與手機瀏覽器檢查；另以虛構 `AQ.` 資料通過真正終端的隱藏輸入、儲存與損毀復原驗證。2026-11-07 已完成真實 Gemini 單次啟用驗證，頁面與 API 均為 `ready`；重啟及修正更新後保留啟用狀態，本機記錄的模型請求與當日用量皆維持 1 次。這項驗證僅涵蓋啟用，知識問答與工具品質評估仍待後續功能交付。

應用程式的 HTTP 存活檢查是 `GET /health`。啟用頁使用 `GET /api/setup`，回傳 `status`、`message`、`model`。驗證由本機命令觸發。
