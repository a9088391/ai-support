# AI 客服工單與知識查詢

目前已實作本機啟動、Gemini 啟用、示範帳號登入登出與角色工作台。FastAPI 同源提供 Nuxt 靜態頁面，並使用 PostgreSQL 18 保存啟用狀態、模型用量與工作階段。知識問答及工單功能將在後續交付。

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

## 示範登入

首次執行 `./bin/local up` 時，會在本機建立 `.local/demo-passwords`（權限 `0600`），為兩個帳號各產生獨立的隨機密碼。由本機操作人員使用本機編輯器開啟此私有檔案。服務啟用後，在登入畫面輸入對應帳號與密碼即可進入工作台。

| 帳號 | 工作台 |
| --- | --- |
| `user` | 一般使用者 |
| `support` | 客服 |

- 重啟會沿用已建立的密碼，PostgreSQL 只保存 Argon2id 雜湊。請妥善保留原始私有檔案。如果檔案遺失或與資料庫不符，請還原原檔及私有權限後再啟動。
- 以每個帳號與連線來源分別計算 15 分鐘時間窗。每個帳號最多 5 次，每個來源最多 20 次。登入成功也計入嘗試次數。達上限後，畫面會請你稍後再試。
- 登入閒置 30 分鐘或累計 8 小時後失效。共用裝置使用完畢後，請在工作台登出。登出會立即撤銷目前的工作階段。

## 開發與測試

```sh
./bin/local check
./bin/local test
./bin/local stop-test
```

`check` 執行 Python 語法、Ruff、mypy 與套件相容性檢查。`test` 使用獨立的 `ai-support-test` 專案與 PostgreSQL 18，涵蓋公開 HTTP 啟用、登入安全、工作階段期限與併發行為，並透過受控 HTTP 傳輸驗證官方 Gemini SDK。測試容器使用合成資料與憑證，測試資料庫在停止時丟棄。

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

模式包括 `unconfigured`、`unverified`、`ready`、`invalid`、`unavailable`（503）、`malformed`（不符合契約）及 `timeout`（超過前端十秒界線）。埠號可省略，預設為 8021，只能綁定本機。這個工具提供前端建置結果與固定測試狀態。`ready` 會顯示未登入的表單，完整登入流程請用 `./bin/local up` 與實際 PostgreSQL 驗證。

目前已通過 56 項後端測試、Python 靜態檢查、前端格式／型別／建置，以及桌面與手機的雙角色登入與登出檢查。先前已以虛構 `AQ.` 資料驗證終端的隱藏輸入、儲存與損毀復原。2026-11-07 已完成一次真實 Gemini 啟用驗證，頁面與 API 均為 `ready`。重啟及更新後保留啟用狀態，本機記錄的模型請求與當日用量皆維持 1 次。知識問答與工具品質評估會隨後續功能驗證。

應用程式的 HTTP 存活檢查是 `GET /health`。啟用頁使用 `GET /api/setup`，回傳 `status`、`message`、`model`。驗證由本機命令觸發。

服務啟用後，`POST /api/login` 接收 JSON 的 `username`、`password`，登入成功後設定 `HttpOnly`、`SameSite=Lax` 的 Cookie。登入與 `GET /api/session` 只回傳 `username`、`role`。`POST /api/logout` 撤銷工作階段並清除 Cookie，即使服務已失去啟用狀態仍可呼叫。若網路中斷而無法確認登出，畫面會提供手動重試。

異動請求需帶同源 `Origin` 及 `X-CSRF-Protection: 1`。`GET /api/session` 同樣需要 `X-CSRF-Protection: 1`，若附帶 `Origin`，它必須同源。查詢預設刷新閒置期限。頁面首次載入使用 `GET /api/session?refresh=false` 確認身分，保留原本的期限。API 請求本文上限為 64 KiB。
