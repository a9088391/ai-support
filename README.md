# AI 客服工單與知識查詢

這是可在本機執行的客服工作台。使用者可以搜尋虛構的「星港工作台」知識文件、向 Gemini 提問及追問、核對引用，並手動把問題轉成工單。客服可以查看工單證據、填寫處理結果並結案，提單者可登入查看結果。

- `frontend/` 使用 Nuxt 4 和 TypeScript，靜態輸出由 FastAPI 同源提供。
- `backend/` 使用 Python、FastAPI 和 Pydantic，透過官方 Google GenAI SDK 的 Interactions API 呼叫 Gemini。模型可使用唯讀知識搜尋。
- PostgreSQL 18 保存啟用狀態、用量、工作階段、知識文件、暫存問答與工單快照。Docker Compose 管理本機服務。

## 啟動與驗證

主機需支援 POSIX 檔案權限與鎖定。本流程以 macOS 搭配 OrbStack 實測。請準備可執行 `sh` 的終端、支援 `up --wait` 的 Docker Compose，以及 Python 3.10 或以上版本（`python3`）。Docker 映像包含後端執行及前端建置環境，主機的 Python 用於本機設定。除非另有說明，命令都從本專案根目錄執行。

```sh
./bin/local up
```

啟動成功後開啟 <http://127.0.0.1:8020/>。沒有設定 Key 也能啟動，畫面會顯示啟用說明。資料庫僅在本案的 Docker 網路內使用。

在 Google AI Studio 登入 Google 帳號，首次使用時依畫面接受服務條款。在 API Keys 頁面可使用預設專案的 Key，或選擇「Create API key」為指定專案建立 Key。確認該 Key 所屬專案的方案是 Free Tier，並確認目前可在免費層使用 `gemini-3.5-flash-lite`，即可在免費額度內示範。 [Google AI Studio API Keys](https://aistudio.google.com/apikey) · [Google Key 設定指南](https://ai.google.dev/gemini-api/docs/api-key) · [計費方案](https://ai.google.dev/gemini-api/docs/billing) · [模型定價](https://ai.google.dev/gemini-api/docs/pricing)

在終端執行以下命令。設定時貼上 Key 並按 Enter，輸入不回顯。驗證成功後，在頁面按「重新確認狀態」進入登入畫面。

```sh
./bin/local set-key
./bin/local verify-key
```

- 本機設定支援新版 `AQ.` Key 格式；儲存成功不代表已啟用，仍須執行 `verify-key`。格式檢查不會顯示輸入內容。[Google 的 Key 格式轉換說明](https://discuss.ai.google.dev/t/bring-back-aiza-format-api-keys/170016)
- 驗證使用 `gemini-3.5-flash-lite`，每次手動操作最多送出一筆簡短的 Gemini Interactions 請求。成功代表當下可使用該模型。 [Gemini 模型說明](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- 設定更換後需要重新驗證。同一設定的成功紀錄在重啟後保留。限流、逾時或供應商暫時錯誤會保留既有驗證狀態。確認憑證無效後，業務 API 會暫停使用。
- 驗證會寫入本機用量帳本，以預留時的臺灣日期記錄。遇到 Gemini `429` 時，終端會顯示限流訊息。模型用量上限由 Google 專案的實際配額決定。 [Gemini API 配額](https://ai.google.dev/gemini-api/docs/rate-limits)
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

## 完整示範

1. 依[啟動與驗證](#啟動與驗證)啟動服務和驗證 Key。執行 `./bin/local import-knowledge` 載入虛構文件。用 `user` 和[示範密碼](#示範登入)登入，搜尋 `export-104` 可先確認已匯入知識。
2. 在「提出問題」送出「我匯出報表時遇到 EXPORT-104，該怎麼處理？」。回答應指出把日期區間縮短至 31 天以內再匯出。在「引用來源」核對 `report-export / v1 / export-104`。
3. 在同一頁輸入「修好之後，去哪裡拿檔案？檔案是什麼格式？」並按「追問」。回答應指向「下載紀錄」及 CSV，引用 `report-export / v1 / export-steps`。
4. 在第 2 題按「建立工單」。在預填描述補上「原始情境是匯出時遇到 EXPORT-104。」後按「送出工單」。記下單號，核對明細的原始問題、描述、回答與來源。
5. 登出並改用 `support` 登入。在「全部工單」找到該單，按「查看工單」。填寫處理結果，例如「已確認報表可在下載紀錄取得，格式為 CSV。」後按「結案」。確認狀態顯示「已結案」。
6. 登出並以 `user` 再登入。在「我的工單」開啟同一單號，核對結案時間、處理結果和先前保存的證據。

## 匯入與搜尋知識文件

以 `./bin/local up` 啟動目前版本後，在專案根目錄執行以下命令，載入隨附的虛構「星港工作台」文件。

```sh
./bin/local import-knowledge
```

服務啟用後，兩種示範帳號都能在工作台搜尋。可試著搜尋 `出報表` 或 `export-104`，查看文件標題、版本、來源識別及純文字摘錄。首次匯入前，查詢會顯示查無符合的片段。搜尋直接讀取 PostgreSQL，不呼叫 Gemini，也不消耗模型額度。

更新時編輯 [knowledge/documents.json](knowledge/documents.json)，再執行同一命令。保留相同文件與片段的穩定 `id`，修改標題、片段文字或權限事實時更新文件的 `version`。同一份檔案可重複匯入。整批成功才替換目前可搜尋的文件，未列在新批次的文件會移除。格式錯誤或資料庫寫入失敗時保留原批，命令會以非零狀態結束。請保留前一版 JSON，需要還原時可重新匯入。

- 批次使用 UTF-8 JSON，最多 4 MiB，須包含 1 到 200 份文件，每份含 1 到 100 個片段。每份文件有 `id`、`title`、`version`、`fragments`，片段有 `id`、`text`。
- 權限片段可加上 `permissions`，以 `role`、`action`、`resource` 及 `effect` 記錄角色可否執行操作。禁止操作的事實可在 `application_recipient` 記錄申請對象。更新時請同步權限事實及可見片段文字；複合權限答覆的每項事實都須有本輪引用的文字支持。
- 識別長度最多 64 字元，須以小寫英文字母或數字開頭，可用小寫英文字母、數字、底線與連字號。文件識別須在整批內唯一，片段識別須在各文件內唯一。標題、版本與片段文字最多分別為 200、64、4,000 個 Unicode 字元。
- 搜尋輸入最多 2,000 個 Unicode 字元，依標題或片段的字面子字串比對，忽略 ASCII 英文字母大小寫及查詢首尾空白。結果依文件與片段識別排序，最多 20 筆。

## 提出問題與查看來源

1. 確認本機服務已啟用並已匯入知識文件，再以任一示範帳號登入。
2. 在「提出問題」輸入最多 2,000 個 Unicode 字元，例如「星港工作台匯出報表遇到 export-104 時該怎麼處理？」。每次手動送出都開始一個新問題。
3. 回答完成後，可在「引用來源」核對標題、文件版本、片段識別與完整摘錄。使用「追問」延續本頁的對話，每段最多 10 個新問題，一次只處理一題。達上限後可手動開始新對話。
4. 若依據不足、輸出未通過驗證、Gemini 回傳 `429`、逾時或供應商暫時無法使用，工作台會顯示原因及該次已取得的搜尋結果，也可手動建立工單。可使用「重試這一題」，沿用原問題並建立新嘗試，不增加題數；每次實際模型請求照常記錄。問答等待期間仍可獨立搜尋知識或登出。

後端透過官方 Google GenAI SDK 的 Interactions API（`store=false`）呼叫 `gemini-3.5-flash-lite`。模型可請求唯讀知識搜尋，後端驗證工具參數及引用是否來自本次實際搜尋。追問使用後端保留的先前成功問答與供應商步驟；每次嘗試仍須有自己的搜尋證據。更換 Key 後需開始新對話。每次嘗試最多 60 秒、3 次模型請求及 2 次知識工具執行。Gemini 專案的速率、Token 和每日配額決定能否外呼。本機按預留時的臺灣日期保存用量，包含啟用驗證、工具往返及手動重試產生的請求。已送出或送達狀態不明的請求保留用量。應用程式不會自動重送問答。

重新整理或離開頁面會清除畫面上的問答，返回後也無法續聊。頁面不保存問答到瀏覽器儲存空間。若連線中斷而無法確認送出結果，畫面會說明情況，且不會自動重送。登出或登入失效後，後端會停止後續模型請求並拒絕交付私人結果。後端暫存於閒置 30 分鐘或建立滿 2 小時時失效，執行中定期清理，重啟時補清停機期間到期資料。清理後保留當日用量。

## 建立、查看與結案

1. 問題受理後，即可在該題選擇「建立工單」。回答成功、失敗或仍在處理時皆可建立。
2. 描述會預填原問題，可編輯至多 4,000 個 Unicode 字元。若是追問，可在描述補上客服需要的背景。
3. 一般使用者到「我的工單」查看自己的單號、狀態、時間、原始問題及結案結果摘要，完整證據在工單明細。客服到「全部工單」查看兩種帳號的工單與保存的證據。客服不能讀取他人原本的對話。
4. 客服開啟工單明細，輸入最多 4,000 個 Unicode 字元的處理結果後送出結案。結案時間以臺灣時間顯示，結果與證據保留供提單者查閱。兩個客服工作階段同時結案時，只有先完成的一筆生效。結案後無法改寫或重開，有後續問題請另外建單。

工單保留原問題與建單當下已完成的證據。回答已完成時保存回答和引用，否則保存已完成的搜尋結果與狀態。處理中建單會結束本機等待，已送出的模型請求仍計入用量。每題只有一張工單。首次建單會核對目前嘗試。原次建單與已核對的重送，在暫存清理後仍能讀回同一張工單與原描述。工單保留建單當時的資料，後續重試、遲到回答、文件改版與暫存清理後仍可查看。

HTTP 呼叫者可使用 `POST /api/tickets` 建單，傳入 `question_id`、`attempt_id` 與 `description`。建單與重送仍只處理登入帳號自己的問答。用 `GET /api/tickets` 及 `GET /api/tickets/{id}` 查閱角色可見的清單與明細。清單以 `limit` 和 `offset` 分頁，每頁最多 100 筆；清單的 `question` 是建單時保存的原始問題。清單及明細的 `status` 為 `open` 或 `closed`；`resolution` 及 UTC `closed_at` 在結案前為 `null`。

客服以 `POST /api/tickets/{id}/close` 傳入 JSON 的 `resolution`。成功回傳更新後的明細；無效本文（包括缺少處理結果）先回傳 `422`；本文有效但由一般帳號送出時回傳 `403`。已結案回傳 `409`。若送出後無法確認結果，可先讀取明細確認狀態。工單 API 沿用啟用與登入驗證，建單和結案也檢查來源及 CSRF。

從僅支援建單的版本升級時，服務啟動會保留原有工單並新增可空的結案欄位；舊工單維持開啟。若已保存結案結果，請用支援 `closed` 的版本讀取資料；舊程式只認得開啟狀態。

`POST /api/answers` 預設回傳終態 JSON。工作台以 `Accept: application/x-ndjson` 接收逐行事件，先從 `accepted` 取得已受理的對話、問題及嘗試識別，再讀取 `result` 或 `error`。問答等待仍受本機期限與連線中斷控制。

## 重設示範工單與問答

本案執行中時，在本機互動式終端執行：

```sh
./bin/local reset-demo
```

請在顯示預覽的同一個終端確認 Docker 目標、容器、磁碟區及資料庫，再輸入 `RESET ai-support` 並按 Enter。確認前可輸入其他文字、結束輸入或按 Ctrl+C 取消。建議先結束工作台上的操作，並在完成後重新整理頁面。

重設清除所有工單（含結案結果）、重送識別及暫存問答。帳號、登入工作階段與登入限制、知識文件、本機設定、啟用紀錄及模型用量帳本都保留。若需復原，請在重設前另行備份。若終端無法確認重設結果，先查核資料與服務狀態再決定是否重試。

## 開發與測試

修改工作台可從 [Nuxt 頁面](frontend/app/pages/index.vue) 與 [元件](frontend/app/components/) 開始，HTTP 入口在 [FastAPI 應用程式](backend/app.py)。修改後執行 `./bin/local up`，重新建置 Docker 映像並以同源方式操作工作台。

```sh
./bin/local check
./bin/local test
./bin/local stop-test
```

`check` 執行 Python 語法、Ruff、mypy 與套件相容性檢查。`test` 使用獨立的 `ai-support-test` 專案與 PostgreSQL 18，涵蓋公開 HTTP 啟用、登入安全、工作階段期限、知識匯入／搜尋、多輪問答、同題重試、工單交易與權限，並透過受控 HTTP 傳輸驗證官方 Gemini SDK。測試容器使用合成資料與憑證，測試資料庫在停止時丟棄。

前端開發使用 Node.js 24.22.2；Docker 建置已鎖定相同版本。使用符合 `frontend/package.json` 的主機 Node.js 後，可另外執行格式、型別及靜態輸出檢查：

```sh
cd frontend
npm ci --no-audit --no-fund
node --test tests/*.test.mjs
npm run format:check
npm run typecheck
npm run generate
```

在 `frontend` 目錄執行以下命令，可於 <http://127.0.0.1:18021/> 檢查啟用畫面的狀態分支，完成後按 `Ctrl+C` 停止：

```sh
node tests/fixture-server.mjs invalid 18021
```

模式包括 `unconfigured`、`unverified`、`ready`、`invalid`、`unavailable`（503）、`malformed`（不符合契約）及 `timeout`（超過前端十秒界線）。埠號可省略，預設為 8021，只能綁定本機。這個工具提供前端建置結果與固定測試狀態。`ready` 會顯示未登入的表單，完整登入流程請用 `./bin/local up` 與實際 PostgreSQL 驗證。

## HTTP API

應用程式的 HTTP 存活檢查是 `GET /health`。啟用頁使用 `GET /api/setup`，回傳 `status`、`message`、`model`。驗證由本機命令觸發。

服務啟用後，`POST /api/login` 接收 JSON 的 `username`、`password`，登入成功後設定 `HttpOnly`、`SameSite=Lax` 的 Cookie。登入與 `GET /api/session` 只回傳 `username`、`role`。`POST /api/logout` 撤銷工作階段並清除 Cookie，即使服務已失去啟用狀態仍可呼叫。若網路中斷而無法確認登出，畫面會提供手動重試。

異動請求需帶同源 `Origin` 及 `X-CSRF-Protection: 1`。`GET /api/session` 同樣需要 `X-CSRF-Protection: 1`，若附帶 `Origin`，它必須同源。查詢預設刷新閒置期限。頁面首次載入使用 `GET /api/session?refresh=false` 確認身分，保留原本的期限。API 請求本文上限為 64 KiB。

`POST /api/knowledge/search` 使用同源及有效的登入 Cookie，送出 JSON 的 `query`。成功回傳 `results` 陣列，每筆包含 `document_id`、`title`、`version`、`fragment_id`、`excerpt`。查無結果時為空陣列。欄位不符回傳 `422`，未登入回傳 `401`，未啟用或目前無法查詢時回傳 `503`。

`POST /api/answers` 需同源及有效的登入 Cookie，本文可為首題 `{"question":"…"}`、追問 `{"question":"…","conversation_id":"…"}`，或手動重試 `{"conversation_id":"…","question_id":"…","attempt_id":"…"}`。重試必須指定該問題目前失敗的嘗試；會回傳相同的對話和問題識別，以及新的嘗試識別。瀏覽器不能提交前文、模型步驟或引用。HTTP `200` 為本次嘗試的終態，包含 `conversation_id`、`question_id`、`attempt_id`、`status`、`answer`、`model`、`citations` 及 `search_results`。引用與搜尋結果使用上述知識片段欄位。只有 `answered` 帶回答與引用，其他終態（`insufficient_evidence`、`invalid_output`、`rate_limited`、`timed_out`、`unavailable`）仍可附上本次搜尋結果。欄位錯誤、未登入、來源驗證失敗及目前無法處理時，分別回傳 `422`、`401`、`403`、`503`。其他帳號或找不到的對話不公開資料，回傳 `404`；對話仍存在但已到期回傳 `410`；忙碌、達上限、過時重試或無可延續前文則回傳 `409`。

`GET /api/answers/{attempt_id}` 可由有效登入的擁有者查閱該問題目前已完成且仍在暫存期限內的結果，不延長期限或再次呼叫模型。識別不存在、已被重試取代或不屬於目前帳號時回傳 `404`，尚未清理的到期結果回傳 `410`。工作台只在當前頁面記住問答識別，不提供歷史列表或恢復入口。

## 實測結果

2026-11-10，隔離 PostgreSQL 的 231 項後端測試及 Python 語法、Ruff、mypy、套件相容性檢查通過。Node.js 24.22.2 的 7 項前端測試、格式、型別檢查和 Nuxt 靜態建置通過。

在 Chrome 桌面 1440px 與手機 320px 視窗，依本文件驗證啟用及登入，並各完成兩輪真實問答、手動建單、客服結案和提單者讀回。畫面上的引用、結案結果與 API 及 PostgreSQL 一致。隔離服務重啟後保留既有工單、啟用狀態和模型用量。

真實 `gemini-3.5-flash-lite` 固定題組通過 20 案、共 23 輪。有依據的 10 案均完整正確（含 3 案兩輪追問），5 案資料不足均未編造，5 案誘導均未觀察到越權。成功回答的引用與本輪知識搜尋結果逐一核對。通過的題組共送出 53 筆問答模型請求，包含工具往返。每輪耗時 7.157 至 18.518 秒，包含驗收時安排的請求間隔。後續修訂以同一批已記錄的模型結果離線重播，23 輪的終態、回答、引用及搜尋結果一致。題組涵蓋隨附虛構知識及專為驗收準備的片段。

### 非模型 API 效能

在專案根目錄執行。確認沒有其他人在使用 `ai-support-perf`；每次須從空的測試資料庫開始。這段命令會印出結果目錄，保留原始 JSON、診斷與退出碼，並清除這次的隔離環境。

```sh
(
  report_dir=$(mktemp -d) || exit
  benchmark_rc=0
  {
    docker compose -p ai-support-perf -f compose.test.yaml build test >&2 &&
    docker compose -p ai-support-perf -f compose.test.yaml run --rm -T test \
      python -m backend.tests.performance > "$report_dir/performance.json"
  } 2> "$report_dir/performance.stderr" || benchmark_rc=$?

  cleanup_rc=0
  docker compose -p ai-support-perf -f compose.test.yaml down --volumes \
    > "$report_dir/cleanup.log" 2>&1 || cleanup_rc=$?
  printf '%s\n' "$benchmark_rc" > "$report_dir/performance.exit"
  printf 'benchmark=%s; cleanup=%s; report_dir=%s\n' "$benchmark_rc" "$cleanup_rc" "$report_dir"
  if [ "$benchmark_rc" -ne 0 ]; then exit "$benchmark_rc"; fi
  exit "$cleanup_rc"
)
```

工具只接受指定的可拋棄 PostgreSQL 18，並拒絕已使用過的資料庫。HTTP 只走測試容器內的 loopback，使用合成帳號與資料，不讀取本機 Gemini Key。從暖機後的 1,000 張工單和 50 份文件開始，每秒送出 10 筆，連續 60 秒。搜尋、清單、明細、首次建單與客服結案各 120 筆，暖機與預期拒絕另外記錄。

JSON 包含寫完 HTTP 請求的送出量、收到完整回應的筆數、各筆時間及錯誤、各類 p95 和資料讀回結果。重測時另記主機規格、Docker 容器配額，並用 `uptime` 和 `docker stats --no-stream` 取樣背景負載。延遲從 HTTP 客戶端開始呼叫算到收完回應本文；搜尋、清單及明細各需 p95 ≤500 ms，建單及結案各需 p95 ≤1,000 ms。送出量不足、超過門檻、非預期錯誤或資料不符，均回傳非零退出碼。

2026-11-08 以 Apple M3（8 核心、24 GiB）、OrbStack（Docker VM 8 vCPU、11.73 GiB）及 PostgreSQL 18.6 實測。測試容器未另設 CPU 或記憶體上限；同機既有容器維持運作，通過輪的主機 1 分鐘負載取樣為 2.19–3.33。

| API | 完成筆數 | p95 |
| --- | ---: | ---: |
| 搜尋 | 120 | 40.65 ms |
| 工單清單 | 120 | 34.89 ms |
| 工單明細 | 120 | 34.35 ms |
| 建立工單 | 120 | 40.83 ms |
| 客服結案 | 120 | 38.08 ms |

共送出並完成 600 筆，每秒 10 筆，沒有非預期 HTTP 或資料錯誤。資料庫讀回 1,120 張工單，其中 121 張已結案（含暖機的一張）；模型請求及配額紀錄均為零。首輪有一個清單時槽未送出，599 筆未達標；以上為修正送出量計量後，以相同負載重跑的結果。本機基準僅反映這組資料、硬體與負載，真實模型延遲另行驗收。

## 使用限制

- 本版供本機展示，HTTP 綁定 loopback。手機版可用同一台電腦的瀏覽器裝置模式檢查。只有兩種固定示範帳號。
- 知識資料透過本機 JSON 匯入。問答品質取決於文件內容、可查得的片段與供應商狀態。固定題組只代表該批案例。遇到資料不足或限流可查看已取得的證據，並手動建單。
- 對話只在目前頁面延續。工單可保留建單時的證據，結案結果唯讀。
