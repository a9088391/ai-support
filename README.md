# AI 客服工單與知識查詢

目前提供本機啟動、Gemini 啟用、示範帳號登入登出、知識搜尋、當頁多輪 AI 問答及同題重試。FastAPI 同源提供 Nuxt 靜態頁面，並使用 PostgreSQL 18 保存啟用狀態、模型用量、工作階段、知識文件與暫存問答。工單功能將在後續交付。

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

## 匯入與搜尋知識文件

以 `./bin/local up` 啟動目前版本後，在專案根目錄執行以下命令，載入隨附的虛構「星港工作台」文件。

```sh
./bin/local import-knowledge
```

服務啟用後，兩種示範帳號都能在工作台搜尋。可試著搜尋 `出報表` 或 `export-104`，查看文件標題、版本、來源識別及純文字摘錄。首次匯入前，查詢會顯示查無符合的片段。搜尋直接讀取 PostgreSQL，不呼叫 Gemini，也不消耗模型額度。

更新時編輯 [knowledge/documents.json](knowledge/documents.json)，再執行同一命令。保留相同文件與片段的穩定 `id`，修改標題或片段內容時更新文件的 `version`。同一份檔案可重複匯入。整批成功才替換目前可搜尋的文件，未列在新批次的文件會移除。格式錯誤或資料庫寫入失敗時保留原批，命令會以非零狀態結束。請保留前一版 JSON，需要還原時可重新匯入。

- 批次使用 UTF-8 JSON，最多 4 MiB，須包含 1 到 200 份文件，每份含 1 到 100 個片段。每份文件有 `id`、`title`、`version`、`fragments`，片段有 `id`、`text`。
- 識別長度最多 64 字元，須以小寫英文字母或數字開頭，可用小寫英文字母、數字、底線與連字號。文件識別須在整批內唯一，片段識別須在各文件內唯一。標題、版本與片段文字最多分別為 200、64、4,000 個 Unicode 字元。
- 搜尋輸入最多 2,000 個 Unicode 字元，依標題或片段的字面子字串比對，忽略 ASCII 英文字母大小寫及查詢首尾空白。結果依文件與片段識別排序，最多 20 筆。

## 提出問題與查看來源

1. 確認本機服務已啟用並已匯入知識文件，再以任一示範帳號登入。
2. 在「提出問題」輸入最多 2,000 個 Unicode 字元，例如「星港工作台匯出報表遇到 export-104 時該怎麼處理？」。每次手動送出都開始一個新問題。
3. 回答完成後，可在「引用來源」核對標題、文件版本、片段識別與完整摘錄。使用「追問」延續本頁的對話，每段最多 10 個新問題，一次只處理一題。達上限後可手動開始新對話。
4. 若依據不足、輸出未通過驗證、額度受限、逾時或供應商暫時無法使用，工作台會顯示原因及該次已取得的搜尋結果。可使用「重試這一題」，沿用原問題並建立新嘗試，不增加題數；每次實際模型請求照常計額。問答等待期間仍可獨立搜尋知識或登出。

後端透過官方 Google GenAI SDK 的 Interactions API（`store=false`）呼叫 `gemini-3.5-flash-lite`。模型可請求唯讀知識搜尋，後端驗證工具參數及引用是否來自本次實際搜尋。追問使用後端保留的先前成功問答與供應商步驟；每次嘗試仍須有自己的搜尋證據。更換 Key 後需開始新對話。每次嘗試最多 60 秒、3 次模型請求及 2 次知識工具執行。每帳號每日最多 20 次業務模型請求，全服務每日最多 100 次模型請求（含啟用驗證），以臺灣時間換日。已送出或送達狀態不明的請求仍保留額度。應用程式不會自動重送問答。

重新整理或離開頁面會清除畫面上的問答，返回後也無法續聊。頁面不保存問答到瀏覽器儲存空間。若連線中斷而無法確認送出結果，畫面會說明情況，且不會自動重送。登出或登入失效後，後端會停止後續模型請求並拒絕交付私人結果。後端暫存於閒置 30 分鐘或建立滿 2 小時時失效，執行中定期清理，重啟時補清停機期間到期資料。清理後保留當日用量。

## 開發與測試

```sh
./bin/local check
./bin/local test
./bin/local stop-test
```

`check` 執行 Python 語法、Ruff、mypy 與套件相容性檢查。`test` 使用獨立的 `ai-support-test` 專案與 PostgreSQL 18，涵蓋公開 HTTP 啟用、登入安全、工作階段期限、知識匯入／搜尋、多輪問答、同題重試與併發行為，並透過受控 HTTP 傳輸驗證官方 Gemini SDK。測試容器使用合成資料與憑證，測試資料庫在停止時丟棄。

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

加入單輪問答前，已通過 68 項後端測試、Python 靜態檢查、前端格式／型別／建置，以及桌面與手機的雙角色登入與登出檢查。先前已以虛構 `AQ.` 資料驗證終端的隱藏輸入、儲存與損毀復原。2026-11-07 已完成一次真實 Gemini 啟用驗證，頁面與 API 均為 `ready`。重啟及更新後保留啟用狀態，本機記錄的模型請求與當日用量皆維持 1 次。知識搜尋另在隔離的 PostgreSQL 18 與 Chrome 桌面、手機視窗驗證。匯入失敗後舊批仍可查，搜尋不新增模型用量。單輪問答加入後，119 項後端測試通過。隔離環境以真實 Gemini 驗收一題虛構的 EXPORT-104 問題，確認模型呼叫知識工具並引用正確片段。該次驗收計入 1 次啟用及 2 次問答請求。完整引用品質題組另行驗收。

加入多輪問答後，138 項隔離 PostgreSQL 測試、Python 靜態檢查及 Node 24.22.2 格式、型別與建置均通過。Chrome 桌面及手機已用真實本機 HTTP、PostgreSQL 和受控 Gemini 傳輸確認追問、引用、同題重試、草稿保留、重整及登出隔離。隔離環境以真實 Gemini 驗收一組 EXPORT-104 首題與追問，兩輪均各自搜尋並正確引用，分別用時 2.004 秒、2.169 秒；共 1 次啟用驗證及 4 次問答請求。

應用程式的 HTTP 存活檢查是 `GET /health`。啟用頁使用 `GET /api/setup`，回傳 `status`、`message`、`model`。驗證由本機命令觸發。

服務啟用後，`POST /api/login` 接收 JSON 的 `username`、`password`，登入成功後設定 `HttpOnly`、`SameSite=Lax` 的 Cookie。登入與 `GET /api/session` 只回傳 `username`、`role`。`POST /api/logout` 撤銷工作階段並清除 Cookie，即使服務已失去啟用狀態仍可呼叫。若網路中斷而無法確認登出，畫面會提供手動重試。

異動請求需帶同源 `Origin` 及 `X-CSRF-Protection: 1`。`GET /api/session` 同樣需要 `X-CSRF-Protection: 1`，若附帶 `Origin`，它必須同源。查詢預設刷新閒置期限。頁面首次載入使用 `GET /api/session?refresh=false` 確認身分，保留原本的期限。API 請求本文上限為 64 KiB。

`POST /api/knowledge/search` 使用同源及有效的登入 Cookie，送出 JSON 的 `query`。成功回傳 `results` 陣列，每筆包含 `document_id`、`title`、`version`、`fragment_id`、`excerpt`。查無結果時為空陣列。欄位不符回傳 `422`，未登入回傳 `401`，未啟用或目前無法查詢時回傳 `503`。

`POST /api/answers` 需同源及有效的登入 Cookie，本文可為首題 `{"question":"…"}`、追問 `{"question":"…","conversation_id":"…"}`，或手動重試 `{"conversation_id":"…","question_id":"…","attempt_id":"…"}`。重試必須指定該問題目前失敗的嘗試；會回傳相同的對話和問題識別，以及新的嘗試識別。瀏覽器不能提交前文、模型步驟或引用。HTTP `200` 為本次嘗試的終態，包含 `conversation_id`、`question_id`、`attempt_id`、`status`、`answer`、`model`、`citations` 及 `search_results`。引用與搜尋結果使用上述知識片段欄位。只有 `answered` 帶回答與引用，其他終態（`insufficient_evidence`、`invalid_output`、`rate_limited`、`timed_out`、`unavailable`）仍可附上本次搜尋結果。欄位錯誤、未登入、來源驗證失敗及目前無法處理時，分別回傳 `422`、`401`、`403`、`503`。其他帳號或找不到的對話不公開資料，回傳 `404`；對話仍存在但已到期回傳 `410`；忙碌、達上限、過時重試或無可延續前文則回傳 `409`。

`GET /api/answers/{attempt_id}` 可由有效登入的擁有者查閱該問題目前已完成且仍在暫存期限內的結果，不延長期限或再次呼叫模型。識別不存在、已被重試取代或不屬於目前帳號時回傳 `404`，尚未清理的到期結果回傳 `410`。工作台只在當前頁面記住問答識別，不提供歷史列表或恢復入口。
