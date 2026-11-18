<script setup lang="ts">
import {
  NDJSON,
  readAnswerStream,
  type ApiResult,
} from "~/workbench/answer-stream";
import {
  SETUP_MODEL,
  INVALID_PLAIN_TEXT_CHARACTER,
  BLANK_PLAIN_TEXT,
  answerStatusMessages,
  answerStatusTitles,
  asRecord,
  isAnswerIdentity,
  isKnowledgeSearchResult,
  readAnswerOutput,
  readTicketDetail,
  readTicketPage,
  sameAnswerAttempt,
  ticketTime,
  validateTicketText,
  type AnswerEvidence,
  type AnswerIdentity,
  type AnswerOutput,
  type KnowledgeSearchResult,
  type Role,
  type TicketDetail,
  type TicketPage,
} from "~/workbench/contracts";

const REQUEST_TIMEOUT_MS = 10_000;
// 模型嘗試最多六十秒，另外保留傳輸與後端結算時間。
const ANSWER_REQUEST_TIMEOUT_MS = 75_000;
const LOGOUT_UNCONFIRMED = "目前無法確認登出結果，請手動重試登出。";
const MAX_KNOWLEDGE_QUERY_CODE_POINTS = 2_000;
const MAX_QUESTION_CODE_POINTS = 2_000;
const MAX_CONVERSATION_QUESTIONS = 10;
const TICKET_PAGE_SIZE = 20;
const TICKET_WRITE_UNCONFIRMED =
  "目前無法確認建單結果，已保留問題描述。可先查看我的工單，或手動重送；若已建單，重送會讀回原工單。";
const ANSWER_LIMIT_MESSAGE = "這段對話已達 10 題上限，請開始新的對話。";
const ANSWER_KEY_CHANGED = "Gemini 設定已變更，請重新提出問題。";
const setupStatuses = [
  "unconfigured",
  "unverified",
  "ready",
  "invalid",
] as const;

type SetupStatus = (typeof setupStatuses)[number];
type AuthState = "checking" | "signed-out" | "signed-in" | "error";

interface SetupDto {
  readonly status: SetupStatus;
  readonly message: string;
  readonly model: typeof SETUP_MODEL;
}

interface IdentityDto {
  readonly username: Role;
  readonly role: Role;
}

interface KnowledgeSearchDto {
  readonly results: readonly KnowledgeSearchResult[];
}

interface AnswerNotice {
  readonly message: string;
  readonly block: "conversation" | "new-question" | "retry" | null;
}

interface AnswerEntry {
  readonly question: string;
  readonly output: AnswerIdentity &
    AnswerEvidence & {
      readonly status:
        AnswerOutput["status"] | "stopped" | "running" | "unconfirmed";
    };
  readonly notice?: AnswerNotice;
}

type KnowledgeSearchState = "idle" | "loading" | "success" | "empty" | "error";

interface Operation {
  readonly controller: AbortController;
  readonly foregroundEpoch: number | null;
}

interface AnswerOperation extends Operation {
  readonly retry: AnswerEntry | null;
  admission: AnswerIdentity | null;
}

interface TicketEditor {
  readonly question: string;
  readonly source: AnswerIdentity;
}

const statusTitles: Record<SetupStatus, string> = {
  unconfigured: "尚未設定 API Key",
  unverified: "設定尚未通過驗證",
  ready: "服務已啟用",
  invalid: "API Key 無效",
};

const setupStatus = ref<SetupDto | null>(null);
const isLoading = ref(false);
const hasSetupError = ref(false);
const authState = ref<AuthState>("checking");
const authMessage = ref("");
const activeOperation = shallowRef<Operation | null>(null);
const logoutPending = ref(false);
let foregroundEpoch = 0;
let lastForegroundCheckEpoch: number | null = null;
let foregroundCheckRequested = false;
let setupReadSequence = 0;
const identity = ref<IdentityDto | null>(null);
const username = ref("");
const password = ref("");
const knowledgeQuery = ref("");
const knowledgeQueryError = ref("");
const knowledgeSearchError = ref("");
const knowledgeSearchState = ref<KnowledgeSearchState>("idle");
const knowledgeResults = ref<KnowledgeSearchResult[]>([]);
const answerQuestion = ref("");
const answerQuestionError = ref("");
const answerNotice = shallowRef<AnswerNotice | null>(null);
const answerEntries = shallowRef<readonly AnswerEntry[]>([]);
const seenAnswerAttempts = new Set<string>();
const answerOperation = shallowRef<AnswerOperation | null>(null);
let admittedQuestionDraft: AnswerIdentity | null = null;
const answerQuestionInput = ref<HTMLTextAreaElement | null>(null);
const answerWorkspace = ref<HTMLElement | null>(null);
const knowledgeSearchWorkspace = ref<HTMLElement | null>(null);
const ticketView = ref<"workbench" | "list" | "detail">("workbench");
const ticketEditor = shallowRef<TicketEditor | null>(null);
const ticketDescription = ref("");
let unconfirmedTicketDescription: string | null = null;
const ticketFormError = ref("");
const ticketDescriptionInvalid = ref(false);
const ticketWrite = shallowRef<Operation | null>(null);
const ticketRead = shallowRef<Operation | null>(null);
const ticketPage = shallowRef<TicketPage | null>(null);
const ticketOffset = ref(0);
const ticketDetailId = ref<string | null>(null);
const ticketDetail = shallowRef<TicketDetail | null>(null);
const ticketReadError = ref("");
const ticketCreated = ref(false);
const ticketDescriptionInput = ref<HTMLTextAreaElement | null>(null);
const ticketFormFeedback = ref<HTMLElement | null>(null);
const ticketHeading = ref<HTMLElement | null>(null);
const ticketReadFeedback = ref<HTMLElement | null>(null);
const ticketResolution = ref("");
const ticketResolutionInvalid = ref(false);
const ticketCloseFeedback = ref("");
const ticketCloseRecovery = ref<"unknown" | "conflict" | null>(null);
const ticketResolutionInput = ref<HTMLTextAreaElement | null>(null);
const ticketCloseFeedbackElement = ref<HTMLElement | null>(null);
const usernameInput = ref<HTMLInputElement | null>(null);
const passwordInput = ref<HTMLInputElement | null>(null);
const knowledgeQueryInput = ref<HTMLInputElement | null>(null);
const knowledgeResultsHeading = ref<HTMLElement | null>(null);
const knowledgeSearchErrorHeading = ref<HTMLElement | null>(null);
const setupStatusHeading = ref<HTMLElement | null>(null);
const setupErrorHeading = ref<HTMLElement | null>(null);
const setupLogoutFeedback = ref<HTMLElement | null>(null);
const authFeedbackHeading = ref<HTMLElement | null>(null);
const workbenchHeading = ref<HTMLElement | null>(null);
const loginFeedbackHeading = ref<HTMLElement | null>(null);
let knowledgeSearchController: AbortController | null = null;
let businessSetupReadback: Operation | null = null;

// 認證忙碌狀態直接由唯一操作推導，避免維護重複旗標。
const isAuthBusy = computed(() => activeOperation.value !== null);
// 前景查詢只暫停送出，不干擾登入欄位的編輯或焦點。
const isAuthActionBusy = computed(
  () => activeOperation.value?.foregroundEpoch === null,
);
// 只有確認 ready 且不在讀取中才可顯示登入或私有工作區。
const isServiceReady = computed(
  () =>
    !isLoading.value &&
    !hasSetupError.value &&
    setupStatus.value?.status === "ready",
);
// 只用伺服器已受理的問題計入題數；重試替換同一題。
const isAnswerAtLimit = computed(
  () =>
    answerEntries.value.length >= MAX_CONVERSATION_QUESTIONS ||
    answerNotice.value?.block === "new-question",
);
// 對話不可延續時仍展示已取得的結果，等使用者明確開始新對話。
const isAnswerConversationBlocked = computed(
  () => answerNotice.value?.block === "conversation",
);
// 工作台標題直接依伺服器回傳角色顯示。
const roleTitle = computed(() =>
  identity.value?.role === "user" ? "一般使用者" : "客服",
);
// 角色工作區文字區分使用者與客服，不顯示未實作的入口。
const roleWorkspace = computed(() =>
  identity.value?.role === "user" ? "個人服務工作區" : "客服處理工作區",
);
// 角色說明只呈現目前角色範圍，不暗示尚未交付的控制項。
const roleDescription = computed(() =>
  identity.value?.role === "user"
    ? "此為您專屬的一般使用者工作區。"
    : "此工作區確認為客服角色，與一般使用者的個人工作區分開。",
);
// 同一套工單導航隨登入角色呈現資料範圍。
const ticketListTitle = computed(() =>
  identity.value?.role === "support" ? "全部工單" : "我的工單",
);
// 確認前留在原工單，避免離開畫面後遺失待核對的草稿。
const isTicketNavigationLocked = computed(
  () => ticketWrite.value !== null || ticketCloseRecovery.value !== null,
);
// 僅接受完整且符合固定模型契約的狀態回應，未知內容不得開放登入。
function isSetupDto(value: unknown): value is SetupDto {
  // 先拒絕非物件與陣列，再安全檢查物件欄位。
  // prettier-ignore
  if (
    typeof value !== "object"
    || value === null
    || Array.isArray(value)
  ) {
    return false;
  }

  const dto = value as Record<string, unknown>;

  // 僅接受明確定義的三個欄位，避免合約外狀態被當成 ready。
  // prettier-ignore
  if (
    Object.keys(dto).length !== 3
    || !("status" in dto)
    || !("message" in dto)
    || !("model" in dto)
  ) {
    return false;
  }

  // 訊息以 Vue 純文字呈現；模型與狀態必須符合本機契約。
  if (typeof dto.message !== "string" || dto.model !== SETUP_MODEL) {
    return false;
  }

  switch (dto.status) {
    case "unconfigured":
    case "unverified":
    case "ready":
    case "invalid":
      return true;
    default:
      return false;
  }
}

// 僅接受相符的示範帳號與角色，避免錯誤身分開啟錯誤工作台。
function isIdentityDto(value: unknown): value is IdentityDto {
  // 先拒絕非物件與陣列，再檢查登入回應欄位。
  // prettier-ignore
  if (
    typeof value !== "object"
    || value === null
    || Array.isArray(value)
  ) {
    return false;
  }

  const dto = value as Record<string, unknown>;

  // 回應僅能包含目前提供的帳號與角色，不接受互不相符的組合。
  return (
    Object.keys(dto).length === 2 &&
    dto.username === dto.role &&
    (dto.role === "user" || dto.role === "support")
  );
}

// 僅接受最多二十筆完整純文字片段，避免錯誤回應進入工作台。
function isKnowledgeSearchDto(value: unknown): value is KnowledgeSearchDto {
  // 先拒絕非物件與陣列，再安全檢查搜尋回應欄位。
  // prettier-ignore
  if (
    typeof value !== "object"
    || value === null
    || Array.isArray(value)
  ) {
    return false;
  }

  const dto = value as Record<string, unknown>;

  // 外層只接受最多二十筆 results，避免錯誤回應進入工作台。
  // prettier-ignore
  if (
    Object.keys(dto).length !== 1
    || !("results" in dto)
    || !Array.isArray(dto.results)
    || dto.results.length > 20
  ) {
    return false;
  }

  // 逐筆拒絕非物件、額外欄位或非文字片段。
  return dto.results.every(isKnowledgeSearchResult);
}

// 只讀取安全狀態與限流標頭，不顯示伺服器錯誤本文或診斷內容。
function getApiFailure(error: unknown): Omit<ApiResult, "data"> {
  // 先確認 FetchError 外形，再安全取得已知的回應欄位。
  if (typeof error !== "object" || error === null || !("response" in error)) {
    return { status: null, retryAfter: null };
  }

  const response = (
    error as {
      response?: {
        status?: unknown;
        headers?: { get?: (name: string) => string | null };
      };
    }
  ).response;

  // 只保留數值狀態碼與 Retry-After，避免顯示服務端任意文字。
  return {
    status: typeof response?.status === "number" ? response.status : null,
    retryAfter: response?.headers?.get?.("Retry-After") ?? null,
  };
}

// 單一操作沿用同一取消權；每個 HTTP 請求各有逾時但不會自動重試。
async function requestApi(
  path: string,
  operation: Operation,
  options: Parameters<typeof $fetch.raw>[1] = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<ApiResult> {
  // 每次請求可逾時，整個操作則由同一取消權涵蓋到 readback 結束。
  const controller = new AbortController();
  // 頁面卸載時將唯一操作的取消傳遞至目前 HTTP 請求。
  const cancelWithOperation = () => controller.abort();
  operation.controller.signal.addEventListener("abort", cancelWithOperation, {
    once: true,
  });
  if (operation.controller.signal.aborted) {
    controller.abort();
  }
  // 單次逾時只中止本次傳輸，操作本身仍可執行安全 readback。
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    // 所有請求同源、停用快取與自動重試，Cookie 由瀏覽器自動管理。
    const response = await $fetch.raw<unknown>(path, {
      ...options,
      cache: "no-store",
      credentials: "same-origin",
      retry: 0,
      signal: controller.signal,
    });
    return {
      status: response.status,
      data: response._data,
      retryAfter: response.headers.get("Retry-After"),
    };
  } catch (error) {
    // 只傳回狀態與限流標頭，呼叫端用安全文案呈現結果。
    return { ...getApiFailure(error), data: null };
  } finally {
    // 清除每次請求的逾時器及操作取消監聽器。
    clearTimeout(timeoutId);
    operation.controller.signal.removeEventListener(
      "abort",
      cancelWithOperation,
    );
  }
}

// 開始唯一的頁面操作，序列化認證動作及其後續狀態回讀。
function beginOperation(
  foregroundEpoch: number | null = null,
): Operation | null {
  // 不允許表單、登出、恢復或 setup 刷新互相覆蓋結果。
  if (activeOperation.value) {
    return null;
  }
  const operation = { controller: new AbortController(), foregroundEpoch };
  activeOperation.value = operation;
  return operation;
}

// 只有目前操作能釋放忙碌狀態，避免舊 finally 清除較新的操作。
function finishOperation(operation: Operation): void {
  // 頁面卸載或新生命週期後，不改寫目前操作擁有權。
  if (activeOperation.value === operation) {
    activeOperation.value = null;
    // 操作結束後才補查較新的前景狀態，保持請求序列。
    if (foregroundCheckRequested) {
      foregroundCheckRequested = false;
      void checkSetupOnForeground();
    }
  }
}

// 等待 Vue 更新畫面後聚焦指定欄位或錯誤標題。
function focus(
  target: { readonly value: HTMLElement | null },
  canFocus?: () => boolean,
): void {
  // 等待目標掛載，再確認畫面仍接受這次鍵盤焦點。
  void nextTick(() => {
    if (!canFocus || canFocus()) {
      target.value?.focus();
    }
  });
}

// 啟用畫面優先聚焦待確認的登出，讓使用者能接續原操作。
function focusSetupResult(focusInactive = true): void {
  // 先告知可手動恢復的登出結果，其次才是一般的啟用說明。
  if (logoutPending.value) {
    focus(setupLogoutFeedback);
  } else if (hasSetupError.value) {
    focus(setupErrorHeading);
  } else if (focusInactive || setupStatus.value?.status === "invalid") {
    focus(setupStatusHeading);
  }
}

// 重新檢查服務狀態；前景查詢維持既有畫面，直到收到失效或無法確認的結果。
async function recheckSetup(
  operation: Operation,
): Promise<"ready" | "inactive" | "unavailable" | "stale"> {
  // 主動確認顯示讀取狀態；前景查詢不重建仍可用的表單或工作台。
  const isForegroundCheck = operation.foregroundEpoch !== null;
  if (!isForegroundCheck) {
    setupStatus.value = null;
    hasSetupError.value = false;
    isLoading.value = true;
  }

  // 較晚開始的 setup 讀取優先，舊回應不得覆蓋新的啟用判斷。
  const readSequence = ++setupReadSequence;

  // 啟用狀態只讀一次；失敗交由使用者手動恢復。
  const result = await requestApi("/api/setup", operation, { method: "GET" });
  if (activeOperation.value !== operation) {
    return "stale";
  }
  // 離開前景前開始的查詢已過期，即使回應有效也不能更新畫面。
  if (isForegroundCheck && operation.foregroundEpoch !== foregroundEpoch) {
    return "stale";
  }
  // 工作台的安全回讀若更新得較晚，舊前景結果不再寫入共享狀態。
  if (readSequence !== setupReadSequence) {
    return "stale";
  }
  isLoading.value = false;

  // 只有有效 ready 能保留前景查詢前的身分與結果；待確認登出另行保留。
  const nextSetup =
    result.status === 200 && isSetupDto(result.data) ? result.data : null;
  setupStatus.value = nextSetup;
  if (!isForegroundCheck || nextSetup?.status !== "ready") {
    identity.value = null;
    authState.value = "checking";
    authMessage.value = "";
  }

  // 無效或不可用的回應不沿用舊 ready 狀態。
  if (!nextSetup) {
    hasSetupError.value = true;
    authState.value = "error";
    return "unavailable";
  }

  // 未啟用時退回原本單欄說明。
  if (nextSetup.status !== "ready") {
    authState.value = "signed-out";
    return "inactive";
  }
  return "ready";
}

// 離開前景便作廢這次回查及排隊中的查詢，等真正返回後再查。
function leaveForeground(): void {
  foregroundEpoch += 1;
  foregroundCheckRequested = false;
}

// 分頁隱藏與視窗失焦都算離開；可見性回復與取得焦點共用查詢。
function handleVisibilityChange(): void {
  if (document.visibilityState === "visible") {
    void checkSetupOnForeground();
  } else {
    leaveForeground();
  }
}

// 回到前景時只確認啟用，避免被動更新工作階段的閒置時間。
async function checkSetupOnForeground(): Promise<void> {
  // 不讓可能隨失焦而作廢的前景查詢搶先取代業務 503 回讀。
  // prettier-ignore
  if (
    document.visibilityState !== "visible"
    || !isServiceReady.value
    || businessSetupReadback
  ) {
    return;
  }

  // 同一次返回只查一次；較新的返回須在目前操作後重新確認。
  if (lastForegroundCheckEpoch === foregroundEpoch) {
    return;
  }
  if (activeOperation.value) {
    foregroundCheckRequested = true;
    return;
  }
  const operation = beginOperation(foregroundEpoch);
  if (!operation) {
    return;
  }
  lastForegroundCheckEpoch = foregroundEpoch;

  // 只有當前查詢確認失效或無法確認時，才聚焦啟用說明。
  const result = await recheckSetup(operation);
  if (activeOperation.value !== operation) {
    return;
  }
  finishOperation(operation);
  if (result === "inactive" || result === "unavailable") {
    focusSetupResult();
  }
}

// 讀取一次 session，將登入、登出及未知結果收斂到同一復原流程。
async function readSession(
  operation: Operation,
  options: {
    readonly messageOnUnauthenticated?: string;
    readonly messageOnUnavailable?: string;
    readonly focusAuthenticated?: boolean;
    readonly focusUnauthenticated?: boolean;
    readonly activationAlreadyChecked?: boolean;
    readonly refresh?: boolean;
  },
): Promise<void> {
  // 同源查詢預設續期；頁面首次載入只確認身分，避免外站導覽延長閒置時間。
  const path =
    options.refresh === false ? "/api/session?refresh=false" : "/api/session";
  const result = await requestApi(path, operation, {
    method: "GET",
    headers: { "X-CSRF-Protection": "1" },
  });
  if (activeOperation.value !== operation) {
    return;
  }

  // 200 只接受已驗證的身分 DTO，再顯示該角色的工作台。
  if (result.status === 200 && isIdentityDto(result.data)) {
    identity.value = result.data;
    authState.value = "signed-in";
    authMessage.value = logoutPending.value
      ? "目前仍維持登入狀態，請手動再次登出。"
      : "";
    finishOperation(operation);
    if (options.focusAuthenticated) {
      focus(workbenchHeading);
    }
    return;
  }

  // 401 明確確認目前未登入，才清除待確認的登出。
  if (result.status === 401) {
    identity.value = null;
    authState.value = "signed-out";
    authMessage.value = logoutPending.value
      ? "已確認目前未登入。"
      : (options.messageOnUnauthenticated ?? "");
    logoutPending.value = false;
    finishOperation(operation);
    if (options.focusUnauthenticated) {
      focus(usernameInput);
    }
    return;
  }

  // 503 先重新確認啟用狀態一次，避免顯示已失效的登入工作區。
  if (result.status === 503 && !options.activationAlreadyChecked) {
    const setupResult = await recheckSetup(operation);
    if (activeOperation.value !== operation) {
      return;
    }
    if (setupResult !== "ready") {
      finishOperation(operation);
      focusSetupResult();
      return;
    }
  }

  // 其他錯誤或未知身分均隱藏私有內容，提供明確手動恢復入口。
  identity.value = null;
  authState.value = "error";
  authMessage.value = logoutPending.value
    ? LOGOUT_UNCONFIRMED
    : (options.messageOnUnavailable ??
      "目前無法確認登入狀態，工作區已隱藏。請手動重新確認登入狀態。");
  finishOperation(operation);
  focus(authFeedbackHeading);
}

// 手動或首次確認啟用狀態；只有 ready 才進一步讀取一次 session。
async function refreshSetupStatus(isManual = false): Promise<void> {
  // 服務刷新與認證操作共用序列鎖，避免新舊結果交錯。
  const operation = beginOperation();
  if (!operation) {
    return;
  }

  // 共用 setup 讀取負責先隱藏舊畫面；此處不呼叫模型。
  const setupResult = await recheckSetup(operation);
  if (activeOperation.value !== operation) {
    return;
  }
  if (setupResult !== "ready") {
    finishOperation(operation);
    focusSetupResult(isManual);
    return;
  }

  // 手動恢復才續期，並在原按鈕被移除後把焦點帶到新畫面。
  await readSession(operation, {
    refresh: isManual,
    focusAuthenticated: isManual,
    focusUnauthenticated: isManual,
  });
}

// 手動重查 session；此函式不輪詢、不重送任何先前異動。
async function checkSession(): Promise<void> {
  // 只有服務 ready 且目前無其他操作時才進行讀取。
  if (!isServiceReady.value) {
    return;
  }
  const operation = beginOperation();
  if (!operation) {
    return;
  }

  // 重新讀取前先隱藏先前身分，讓逾時結果不留下舊私有畫面。
  identity.value = null;
  authState.value = "checking";
  authMessage.value = "";
  await readSession(operation, {
    messageOnUnauthenticated: "目前未登入，請使用示範帳號登入。",
    messageOnUnavailable:
      "目前仍無法確認登入狀態，工作區已隱藏。請稍後手動重試。",
    focusAuthenticated: true,
    focusUnauthenticated: true,
  });
}

// 送出登入並清除密碼；未知結果只讀回 session，不重送登入。
async function submitLogin(): Promise<void> {
  // 原生必填驗證先執行，且只在服務 ready 時接受登入操作。
  if (!isServiceReady.value) {
    return;
  }
  const operation = beginOperation();
  if (!operation) {
    return;
  }

  // 送出前複製欄位並清除畫面密碼，避免任何結果後殘留。
  const submittedUsername = username.value;
  const submittedPassword = password.value;
  password.value = "";
  identity.value = null;
  authState.value = "checking";
  authMessage.value = "";

  // 登入是明確的同源 JSON 異動，包含核准的 CSRF 標頭且不自動重試。
  const result = await requestApi("/api/login", operation, {
    method: "POST",
    body: { username: submittedUsername, password: submittedPassword },
    headers: { "X-CSRF-Protection": "1" },
  });
  if (activeOperation.value !== operation) {
    return;
  }

  // 只有有效 200 身分回應才直接開啟工作台。
  if (result.status === 200 && isIdentityDto(result.data)) {
    identity.value = result.data;
    authState.value = "signed-in";
    finishOperation(operation);
    focus(workbenchHeading);
    return;
  }

  // 明確錯誤顯示安全且可行動的訊息，不透露帳號是否存在。
  if ([401, 403, 422, 429].includes(result.status ?? 0)) {
    authState.value = "signed-out";
    if (result.status === 401) {
      authMessage.value = "帳號或密碼不正確，請重新輸入密碼。";
      finishOperation(operation);
      focus(passwordInput);
    } else if (result.status === 422) {
      authMessage.value = "登入欄位格式不正確，請檢查示範帳號與密碼。";
      finishOperation(operation);
      focus(usernameInput);
    } else if (result.status === 429) {
      const seconds = Number(result.retryAfter);
      authMessage.value =
        Number.isSafeInteger(seconds) && seconds > 0
          ? `登入嘗試已達限制，請至少等待 ${seconds} 秒後再手動重試。`
          : "登入嘗試已達限制，請稍後再手動重試。";
      finishOperation(operation);
      focus(loginFeedbackHeading);
    } else {
      authMessage.value =
        "目前無法驗證登入要求來源，請重新載入本機頁面後再試。";
      finishOperation(operation);
      focus(loginFeedbackHeading);
    }
    return;
  }

  // 503 先查啟用；其他網路、逾時及格式未知結果只做一次 session readback。
  if (result.status === 503) {
    const setupResult = await recheckSetup(operation);
    if (activeOperation.value !== operation) {
      return;
    }
    if (setupResult !== "ready") {
      finishOperation(operation);
      focusSetupResult();
      return;
    }
  }
  await readSession(operation, {
    messageOnUnauthenticated: "登入結果無法確認；目前未登入，請手動重新登入。",
    messageOnUnavailable:
      "目前無法確認登入結果，工作區已隱藏。請手動重新確認登入狀態。",
    focusAuthenticated: true,
    focusUnauthenticated: true,
    activationAlreadyChecked: result.status === 503,
  });
}

// 取消舊查詢並清除其結果，避免失效回應或片段跨身分留存。
function cancelKnowledgeSearch(): void {
  // 先撤銷目前控制器，再中止傳輸，讓遲到回應無法更新畫面。
  const controller = knowledgeSearchController;
  knowledgeSearchController = null;
  controller?.abort();

  // 清除與舊查詢相關的畫面狀態，保留使用者正在編輯的關鍵字。
  knowledgeSearchState.value = "idle";
  knowledgeSearchError.value = "";
  knowledgeQueryError.value = "";
  knowledgeResults.value = [];
}

// 只有同一身分與啟用狀態仍有效時，才允許查詢結果更新工作台。
function isCurrentKnowledgeSearch(
  operation: Operation,
  owner: IdentityDto,
): boolean {
  // 同時檢查控制器、取消權、服務狀態與登入物件擁有權。
  // prettier-ignore
  return (
    knowledgeSearchController === operation.controller
    && !operation.controller.signal.aborted
    && isServiceReady.value
    && identity.value === owner
  );
}

// 修改關鍵字時中止舊查詢並移除不再對應的結果與錯誤。
function handleKnowledgeQueryInput(): void {
  // 輸入變更代表目前結果已不再符合表單內容。
  cancelKnowledgeSearch();
}

// 業務操作收到 401 時統一清除身分，並讓鍵盤使用者回到登入表單。
function expireWorkbenchSession(message: string): void {
  // 同步撤銷身分也會取消該工作階段尚未完成的搜尋及問答。
  logoutPending.value = false;
  identity.value = null;
  authState.value = "signed-out";
  authMessage.value = message;
  focus(usernameInput);
}

// 業務 API 的 503 只讀回一次啟用狀態，由最新回讀決定整個工作台是否可用。
async function recheckBusinessSetup(): Promise<"ready" | "stale" | "blocked"> {
  // 回讀取代同一前景週期的檢查，避免較早的狀態覆蓋此次復原結果。
  foregroundEpoch += 1;
  lastForegroundCheckEpoch = foregroundEpoch;
  foregroundCheckRequested = false;
  const foregroundOperation = activeOperation.value;
  if (foregroundOperation && foregroundOperation.foregroundEpoch !== null) {
    // 先釋放前景讀取擁有權再取消，讓登出在回讀期間仍可執行。
    activeOperation.value = null;
    foregroundOperation.controller.abort();
  }

  // 回讀屬於工作階段；編輯查詢或問題只影響各自的畫面操作。
  const readback: Operation = {
    controller: new AbortController(),
    foregroundEpoch: null,
  };
  businessSetupReadback?.controller.abort();
  businessSetupReadback = readback;
  const readSequence = ++setupReadSequence;
  const result = await requestApi("/api/setup", readback, { method: "GET" });

  // 只釋放自己的回讀；登出、卸載或較新的讀取都會撤銷共享狀態更新權。
  if (businessSetupReadback !== readback) {
    return "stale";
  }
  businessSetupReadback = null;
  if (
    readback.controller.signal.aborted ||
    readSequence !== setupReadSequence
  ) {
    return "stale";
  }

  // 只有明確的 ready 能保留工作台；未啟用或無法確認都隱藏私人資料。
  const nextSetup =
    result.status === 200 && isSetupDto(result.data) ? result.data : null;
  if (nextSetup?.status === "ready") {
    return "ready";
  }
  setupStatus.value = nextSetup;
  hasSetupError.value = nextSetup === null;
  identity.value = null;
  authState.value = nextSetup ? "signed-out" : "error";
  authMessage.value = "";
  focusSetupResult();
  return "blocked";
}

// 搜尋已匯入知識片段，使用獨立取消權，不佔用登入或登出的操作鎖。
async function submitKnowledgeSearch(): Promise<void> {
  // 僅允許 ready 且已登入的角色使用工作台搜尋。
  const owner = identity.value;
  if (!isServiceReady.value || !owner) {
    return;
  }

  // 空白查詢明確要求修正，不依賴 required 接受空格的瀏覽器行為。
  const query = knowledgeQuery.value;
  if (!query.trim()) {
    cancelKnowledgeSearch();
    knowledgeQueryError.value = "請輸入搜尋關鍵字。";
    focus(knowledgeQueryInput);
    return;
  }

  // 以 Unicode code points 計數，超限時拒絕而不截斷 UTF-16 輸入。
  if (Array.from(query).length > MAX_KNOWLEDGE_QUERY_CODE_POINTS) {
    cancelKnowledgeSearch();
    knowledgeQueryError.value = "搜尋關鍵字不可超過 2,000 個 Unicode 字元。";
    focus(knowledgeQueryInput);
    return;
  }

  // 新送出會先撤銷舊查詢，並清除與前次關鍵字相關的畫面內容。
  cancelKnowledgeSearch();
  const operation: Operation = {
    controller: new AbortController(),
    foregroundEpoch: null,
  };
  knowledgeSearchController = operation.controller;
  knowledgeSearchState.value = "loading";

  try {
    // 查詢只送原始關鍵字與 CSRF 標頭，同源 fetch 不會自動重送。
    const result = await requestApi("/api/knowledge/search", operation, {
      method: "POST",
      body: { query },
      headers: { "X-CSRF-Protection": "1" },
    });
    if (!isCurrentKnowledgeSearch(operation, owner)) {
      return;
    }

    // 只有完整且合法的 200 回應可以顯示片段，查無資料仍是有效結果。
    if (result.status === 200) {
      if (!isKnowledgeSearchDto(result.data)) {
        knowledgeSearchError.value = "搜尋回應格式無法確認，請稍後手動重試。";
        knowledgeSearchState.value = "error";
        focus(knowledgeSearchErrorHeading);
        return;
      }

      knowledgeResults.value = [...result.data.results];
      knowledgeSearchState.value =
        result.data.results.length === 0 ? "empty" : "success";
      focus(knowledgeResultsHeading);
      return;
    }

    // 明確 401 立即交回既有登入畫面，並把焦點放回帳號欄位。
    if (result.status === 401) {
      expireWorkbenchSession("登入工作階段已失效，請重新登入後搜尋。");
      return;
    }

    // 固定安全錯誤不顯示服務端本文，保留表單供使用者修正或重試。
    if (result.status === 422) {
      knowledgeSearchError.value = "搜尋關鍵字未通過格式檢查，請修正後重試。";
    } else if (result.status === 403) {
      knowledgeSearchError.value =
        "搜尋要求未通過來源驗證，請重新載入本機頁面後重試。";
    } else if (result.status === 503) {
      // 仍是原搜尋才顯示手動重試；遲到的回讀不改動新查詢或焦點。
      const setupResult = await recheckBusinessSetup();
      if (!isCurrentKnowledgeSearch(operation, owner)) {
        return;
      }
      knowledgeSearchError.value =
        setupResult === "ready"
          ? "知識搜尋暫時無法使用，服務仍已啟用。請稍後手動重試。"
          : "知識搜尋暫時無法完成，請稍後手動搜尋。";
    } else {
      // 網路失敗、逾時及其他未知狀態都使用安全文案且不自動重試。
      knowledgeSearchError.value =
        "目前無法完成知識搜尋，請檢查本機服務後手動重試。";
    }

    // 錯誤保留在搜尋區並聚焦標題，讓鍵盤使用者直接讀取復原方式。
    knowledgeSearchState.value = "error";
    focus(knowledgeSearchErrorHeading);
  } finally {
    // 只釋放本次控制器參照，不讓舊查詢覆寫較新的搜尋狀態。
    if (knowledgeSearchController === operation.controller) {
      knowledgeSearchController = null;
    }
  }
}

// 清除本頁對話並取消等待，避免遲到結果還原已清除的內容。
function clearAnswer(keepDraft = false): void {
  // 先撤銷畫面更新權，再中止傳輸，遲到結果不能復原已清除的內容。
  const operation = answerOperation.value;
  answerOperation.value = null;
  operation?.controller.abort();

  // 只有同一工作台內明確開始新對話，才保留尚未送出的草稿。
  if (!keepDraft) {
    answerQuestion.value = "";
  }
  answerQuestionError.value = "";
  answerNotice.value = null;
  answerEntries.value = [];
  admittedQuestionDraft = null;
  seenAnswerAttempts.clear();
}

// 使用者明確放棄前文後才回到第一題，並讓焦點回到仍可編輯的草稿。
function startNewConversation(): void {
  // 防止鍵盤或程式事件在已送出的異動還未收斂前清除對話。
  // prettier-ignore
  if (
    !isServiceReady.value
    || !identity.value
    || answerOperation.value
    || ticketEditor.value
  ) {
    return;
  }
  clearAnswer(true);
  focus(answerQuestionInput);
}

// 離開頁面也撤銷共用安全回讀，避免快取頁回來時套用舊的私有操作。
function clearPageBusinessState(): void {
  // 快取返回後須重新檢查啟用，不能沿用離開前開始的檢查。
  leaveForeground();
  const readback = businessSetupReadback;
  businessSetupReadback = null;
  readback?.controller.abort();

  // 搜尋及問答草稿與結果都不應由瀏覽器返回還原。
  cancelKnowledgeSearch();
  knowledgeQuery.value = "";
  clearAnswer();
  clearTickets();
}

// 離開時也撤銷認證讀取；快取還原後不沿用離開前的身分。
function handlePageHide(): void {
  // 撤銷操作再清空狀態，避免舊回應在頁面返回後套用。
  const operation = activeOperation.value;
  activeOperation.value = null;
  operation?.controller.abort();
  clearPageBusinessState();

  // 快取頁面先隱藏私人區域，返回時再讀取啟用與身分。
  setupStatus.value = null;
  identity.value = null;
  authState.value = "checking";
  authMessage.value = "";
}

// 從瀏覽器快取返回時重新確認身分，不延長閒置期限或復原舊對話。
function handlePageShow(event: PageTransitionEvent): void {
  if (event.persisted) {
    void refreshSetupStatus();
  } else {
    void checkSetupOnForeground();
  }
}

// 只有原本的登入身分與等待操作可以接收本次問答。
function isCurrentAnswer(operation: Operation, owner: IdentityDto): boolean {
  // 頁面離開、登出或失去啟用後都不能交付私人結果。
  // prettier-ignore
  return (
    answerOperation.value === operation
    && !operation.controller.signal.aborted
    && isServiceReady.value
    && identity.value === owner
  );
}

// 使用與後端一致的純文字及 Unicode 上限，超限直接拒絕而不截斷。
function validateQuestion(question: string): string {
  // 先以 UTF-16 長度快速排除過長輸入，再以實際 Unicode 碼位精確計數。
  if (
    question.length > MAX_QUESTION_CODE_POINTS * 2 ||
    Array.from(question).length > MAX_QUESTION_CODE_POINTS
  ) {
    return "問題不可超過 2,000 個 Unicode 字元。";
  }

  // 無效控制字元及未配對的代理字元不可送往後端。
  if (INVALID_PLAIN_TEXT_CHARACTER.test(question)) {
    return "問題包含無法使用的字元，請修正後再提出。";
  }

  // 拒絕後端視為空白的文字，避免對空問題送出問答請求。
  return BLANK_PLAIN_TEXT.test(question) ? "請輸入您的問題。" : "";
}

// 草稿一經編輯便與先前的問答脫鉤，遲到的建單不得清除新的文字。
function handleAnswerQuestionInput(): void {
  admittedQuestionDraft = null;
  answerQuestionError.value = "";
}

// 僅辨識問答契約的固定錯誤；其他回應不能證明送出是否已生效。
function readAnswerFailure(result: ApiResult, isRetry: boolean): AnswerNotice {
  // 以固定比對辨識可安全恢復的狀態，不呈現任意伺服器錯誤文字。
  const data = result.data;
  const detail =
    typeof data === "object" && data !== null && "detail" in data
      ? data.detail
      : null;
  if (result.status === 409) {
    if (detail === ANSWER_LIMIT_MESSAGE) {
      return { message: ANSWER_LIMIT_MESSAGE, block: "new-question" };
    }
    if (detail === "這段對話正在處理問題，請稍後再試。") {
      return { message: detail, block: null };
    }
    if (detail === "這段對話缺少可延續的前文，請開始新的對話。") {
      return { message: detail, block: "conversation" };
    }
    if (isRetry && detail === "這次問答已無法重試。") {
      return { message: detail, block: "retry" };
    }
  }

  // 對話無法存取或沿用時仍保留本頁已知結果，讓使用者自行開始新的對話。
  if (result.status === 410 && detail === "問答暫存已到期。") {
    return { message: detail, block: "conversation" };
  }
  if (result.status === 404 && detail === "找不到這次問答。") {
    return { message: detail, block: "conversation" };
  }
  if (result.status === 503 && detail === ANSWER_KEY_CHANGED) {
    return { message: ANSWER_KEY_CHANGED, block: "conversation" };
  }

  // 格式或來源拒絕不等同於可重試的失敗終態。
  if (result.status === 422 && isRetry) {
    return { message: "重試要求未通過格式檢查。", block: "retry" };
  }
  if (result.status === 403) {
    return {
      message: "問答要求未通過來源驗證，請重新載入本機頁面後再試。",
      block: null,
    };
  }

  // 遺失終態就沒有可驗證的新識別，不能重送或冒稱後端未執行。
  return isRetry
    ? {
        message:
          "無法確認重試結果。伺服器可能已完成重試，後續問答的前文可能與本頁顯示不同；此題不能再重試。",
        block: "retry",
      }
    : {
        message:
          "無法確認送出結果。伺服器可能已處理這一題。已保留草稿；請先開始新的對話。",
        block: "conversation",
      };
}

// 驗證終態識別屬於本次操作，並拒絕重複採用已看過的嘗試。
function matchesAnswerOperation(
  output: AnswerIdentity,
  conversationId: string | undefined,
  retry: AnswerEntry | null,
): boolean {
  // 首題沒有預先識別；追問及重試必須回到正在使用的對話。
  if (
    seenAnswerAttempts.has(output.attempt_id) ||
    (conversationId && output.conversation_id !== conversationId)
  ) {
    return false;
  }
  // 重試只能替換原題；追問則必須回傳一個未見過的新問題。
  return retry
    ? output.question_id === retry.output.question_id
    : !answerEntries.value.some(
        // 問題識別是清單鍵；拒絕讓追問覆蓋先前的卡片。
        (entry) => entry.output.question_id === output.question_id,
      );
}

// POST 只送本次問題或重試識別；同一個逾時涵蓋標頭及串流本文。
async function requestAnswer(
  body: object,
  operation: AnswerOperation,
  accept: (frame: Record<string, unknown>) => boolean,
): Promise<ApiResult> {
  // 單次逾時中止傳輸；整個操作仍可用原控制器安全讀回。
  const timeout = AbortSignal.timeout(ANSWER_REQUEST_TIMEOUT_MS);
  const signal = AbortSignal.any([operation.controller.signal, timeout]);

  try {
    // 原生 fetch 直接讀取資料流，保留原本的 JSON 拒絕狀態。
    const response = await fetch("/api/answers", {
      method: "POST",
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: NDJSON,
        "Content-Type": "application/json",
        "X-CSRF-Protection": "1",
      },
      signal,
    });
    if (response.status !== 200) {
      // 即使本文不是 JSON，仍保留 HTTP 狀態給認證及啟用檢查。
      const data: unknown = await response.json().catch(
        // 代理或斷線可能沒有錯誤本文；不將解析診斷交給畫面。
        () => null,
      );
      return {
        status: response.status,
        data,
        retryAfter: response.headers.get("Retry-After"),
      };
    }

    // 拒絕協商錯誤或空串流，避免當成正常受理。
    if (response.headers.get("Content-Type") !== NDJSON || !response.body) {
      await response.body?.cancel();
      return { status: null, data: null, retryAfter: null };
    }
    return await readAnswerStream(response.body, accept);
  } catch {
    // 逾時、取消或斷線都不能證明伺服器沒有受理。
    return { status: null, data: null, retryAfter: null };
  }
}

// 更新已知題目的當前狀態，不改動同一段對話的順序。
function storeAnswerEntry(position: number, entry: AnswerEntry): void {
  const entries = [...answerEntries.value];
  entries[position] = entry;
  answerEntries.value = entries;
}

// 送出首題、追問或同題重試；共用等待鎖但不佔用搜尋與登出的操作。
async function submitAnswer(retry: AnswerEntry | null = null): Promise<void> {
  // 由同一個登入物件和操作識別持有結果更新權；建單編輯期間不更換來源嘗試。
  const owner = identity.value;
  // prettier-ignore
  if (
    !isServiceReady.value
    || !owner
    || answerOperation.value
    || isAnswerConversationBlocked.value
    || ticketEditor.value
  ) {
    return;
  }

  // 重試只能使用目前仍可重試的卡片；本地題數達限不影響重試。
  const position = retry
    ? answerEntries.value.indexOf(retry)
    : answerEntries.value.length;
  // prettier-ignore
  if (
    retry
    && (
      position < 0
      || retry.output.status === "answered"
      || retry.output.status === "unconfirmed"
      || retry.notice?.block === "retry"
    )
  ) {
    return;
  }

  // 驗證新題草稿；不因重試清除或檢查下一題的輸入。
  const question = retry?.question ?? answerQuestion.value;
  if (!retry) {
    if (isAnswerAtLimit.value) {
      return;
    }
    answerQuestionError.value = validateQuestion(question);
    if (answerQuestionError.value) {
      focus(answerQuestionInput);
      return;
    }
    answerNotice.value = null;
  }

  // 重試只傳原本的識別，讓後端使用可信題目與前文；追問只送新題。
  const conversationId = answerEntries.value[0]?.output.conversation_id;
  const body = retry
    ? {
        conversation_id: retry.output.conversation_id,
        question_id: retry.output.question_id,
        attempt_id: retry.output.attempt_id,
      }
    : conversationId
      ? { question, conversation_id: conversationId }
      : { question };
  const operation: AnswerOperation = {
    controller: new AbortController(),
    foregroundEpoch: null,
    retry,
    admission: null,
  };
  answerOperation.value = operation;
  let focusId = `answer-result-${position}`;

  try {
    // 只在伺服器承諾受理且識別符合成員關係後，開放當前卡片的建單。
    let result = await requestAnswer(body, operation, (frame) => {
      // 已離開的登入或操作不得採用遲到的受理，也不能替換既有問題。
      // prettier-ignore
      if (
        !isCurrentAnswer(operation, owner)
        || !isAnswerIdentity(frame)
        || !matchesAnswerOperation(frame, conversationId, retry)
      ) {
        return false;
      }

      // 複製核對後的識別；首次及追問增加一題，重試佔同一位置。
      const { conversation_id, question_id, attempt_id } = frame;
      const admission = { conversation_id, question_id, attempt_id };
      operation.admission = admission;
      if (!retry) {
        admittedQuestionDraft = admission;
      }
      seenAnswerAttempts.add(attempt_id);
      storeAnswerEntry(position, {
        question,
        output: {
          ...admission,
          status: "running",
          answer: null,
          citations: [],
          search_results: [],
        },
      });
      return true;
    });
    if (!isCurrentAnswer(operation, owner)) {
      return;
    }

    // 丟失或無效的終態只讀回一次，不重送 POST，也不採用不同嘗試的資料。
    const admission = operation.admission;
    let output = result.status === 200 ? readAnswerOutput(result.data) : null;
    // prettier-ignore
    if (
      admission
      && (result.status === null || result.status === 200)
      && (!output || !sameAnswerAttempt(output, admission))
    ) {
      result = await requestApi(`/api/answers/${admission.attempt_id}`, operation, { method: "GET", ignoreResponseError: true });
      if (!isCurrentAnswer(operation, owner)) {
        return;
      }
      output = result.status === 200 ? readAnswerOutput(result.data) : null;
    }

    // 身分到期時清除整個私人工作台並回到登入。
    if (result.status === 401) {
      expireWorkbenchSession("登入工作階段已失效，請重新登入後提出問題。");
      return;
    }

    // 啟用讀回保護整個工作台；仍需核對本次問答沒有被工單結束。
    if (result.status === 503) {
      await recheckBusinessSetup();
      if (!isCurrentAnswer(operation, owner)) {
        return;
      }
    }

    // 後端格式拒絕後保留草稿供修正。
    // prettier-ignore
    if (
      !admission
      && result.status === 422
      && !retry
    ) {
      answerQuestionError.value = "問題未通過格式檢查，請修正後再提出。";
      if (ticketView.value === "workbench" && !ticketEditor.value) {
        focus(answerQuestionInput);
      }
      return;
    }

    // 終態只能更新已受理的同一組識別，不能建立或換掉另一題。
    // prettier-ignore
    if (
      admission
      && output
      && sameAnswerAttempt(output, admission)
    ) {
      storeAnswerEntry(position, { question, output });
      if (!retry) {
        admittedQuestionDraft = null;
        answerQuestion.value = "";
      }
    } else if (admission) {
      // 受理後的 404 也可能是建單已提交、回應尚未到達。保留可建單的原識別，
      // 讓建單成功補回停等狀態，不把可繼續的對話誤判為不存在。
      const entry = answerEntries.value[position];
      if (entry?.output.status === "running") {
        storeAnswerEntry(position, {
          ...entry,
          output: { ...entry.output, status: "unconfirmed" },
        });
      }

      // 只有明確失效契約才限制已確認的對話；其他失敗保留題卡供復原。
      const detail = asRecord(result.data)?.detail;
      if (
        (result.status === 410 && detail === "問答暫存已到期。") ||
        (result.status === 503 && detail === ANSWER_KEY_CHANGED)
      ) {
        answerNotice.value = readAnswerFailure(result, retry !== null);
        focusId = "answer-notice-title";
      }
    } else {
      // 尚未受理的局部重試錯誤跟著原卡；整段對話限制放在提問區。
      const notice = readAnswerFailure(result, retry !== null);
      // prettier-ignore
      if (
        retry
        && notice.block !== "conversation"
        && notice.block !== "new-question"
      ) {
        storeAnswerEntry(position, { ...retry, notice });
        focusId = `answer-retry-notice-${position}`;
      } else {
        answerNotice.value = notice;
        focusId = "answer-notice-title";
      }
    }
  } finally {
    // 只釋放本次等待；清除頁面或建單後開始的新操作不受舊結果影響。
    if (answerOperation.value === operation) {
      answerOperation.value = null;
    }
  }

  // 搜尋雖與問答共用版面，仍排除整個搜尋區，保留輸入、結果及錯誤標題焦點。
  const entries = answerEntries.value;
  await nextTick();
  // prettier-ignore
  if (
    identity.value === owner
    && isServiceReady.value
    && answerEntries.value === entries
    && !answerOperation.value
    && !ticketEditor.value
    && ticketView.value === "workbench"
    && !knowledgeSearchWorkspace.value?.contains(document.activeElement)
    && (document.activeElement === document.body || answerWorkspace.value?.contains(document.activeElement))
  ) {
    document.getElementById(focusId)?.focus();
  }
}

// 清除草稿與回饋只用於明確關閉、確認建單或離開登入。
function resetTicketEditor(): void {
  ticketEditor.value = null;
  ticketDescription.value = "";
  unconfirmedTicketDescription = null;
  ticketFormError.value = "";
  ticketDescriptionInvalid.value = false;
}

// 離開工單或帳號、或確認已結案後清除該張工單的私人草稿。
function resetTicketResolution(): void {
  ticketResolution.value = "";
  ticketResolutionInvalid.value = false;
  ticketCloseFeedback.value = "";
  ticketCloseRecovery.value = null;
}

// 清除前一工作階段的表單、清單和快照，撤銷所有遲到回應的更新權。
function clearTickets(): void {
  // 先撤銷目前操作，再取消讀寫傳輸；取消 POST 不能推定未提交。
  const write = ticketWrite.value;
  const read = ticketRead.value;
  ticketWrite.value = null;
  ticketRead.value = null;
  write?.controller.abort();
  read?.controller.abort();

  // 同一頁返回也不展示舊帳號的資料或草稿。
  resetTicketEditor();
  resetTicketResolution();
  ticketView.value = "workbench";
  ticketPage.value = null;
  ticketDetail.value = null;
  ticketDetailId.value = null;
  ticketOffset.value = 0;
  ticketReadError.value = "";
  ticketCreated.value = false;
}

// 建單表單鎖定畫面已確認的題目和嘗試；草稿與原題分開保存。
function openTicketEditor(entry: AnswerEntry): void {
  // 舊嘗試正在換成新嘗試時，等收到本次受理再允許建單。
  // prettier-ignore
  if (
    !identity.value
    || !isServiceReady.value
    || ticketEditor.value
    || !answerEntries.value.includes(entry)
    || answerOperation.value?.retry === entry
  ) {
    return;
  }

  // 只綁定不透明識別；不把畫面的回答或來源放進寫入本文。
  const { conversation_id, question_id, attempt_id } = entry.output;
  ticketEditor.value = {
    question: entry.question,
    source: { conversation_id, question_id, attempt_id },
  };
  ticketDescription.value = entry.question;
  focus(ticketDescriptionInput);
}

// 明確取消後回到對應題目的原控制項；寫入未完成時不關閉草稿。
async function closeTicketEditor(): Promise<void> {
  const editor = ticketEditor.value;
  const owner = identity.value;
  if (!editor || ticketWrite.value) {
    return;
  }
  resetTicketEditor();

  // 等待按鈕解除停用，並避免在工作台已切換後聚焦。
  await nextTick();
  if (identity.value === owner && ticketView.value === "workbench") {
    document
      .getElementById(`create-ticket-${editor.source.question_id}`)
      ?.focus();
  }
}

// 草稿仍可編輯；更正文字後只解除本地欄位錯誤，未知寫入的回饋仍應保留。
function handleTicketDescriptionInput(): void {
  if (ticketDescriptionInvalid.value) {
    ticketDescriptionInvalid.value = false;
    ticketFormError.value = "";
  }
}

// 讀取和寫入各持有自己的更新權，回讀後仍要核對原登入身分。
function isCurrentTicket(
  operation: Operation,
  owner: IdentityDto,
  current: Operation | null,
): boolean {
  // prettier-ignore
  return (
    current === operation
    && !operation.controller.signal.aborted
    && isServiceReady.value
    && identity.value === owner
  );
}

// 固定回覆說明可操作的復原方式，不呈現任意服務端錯誤內容。
function ticketFailureMessage(status: number | null, isWrite: boolean): string {
  switch (status) {
    case 403:
      return "工單要求未通過來源驗證，請重新載入本機頁面後再試。";
    case 404:
      return isWrite ? "找不到指定的問答或工單。" : "找不到這張工單。";
    case 409:
      return "這次問答已更新，請返回問答卡使用目前的嘗試。";
    case 410:
      return "問答暫存已到期，無法從這次問答建立新工單。";
    case 422:
      return "問題描述未通過格式檢查，請修正後再送出。";
    default:
      return isWrite
        ? TICKET_WRITE_UNCONFIRMED
        : "目前無法讀取工單，請稍後手動重試。";
  }
}

// 只有同一題且同一次嘗試的快照能補上問答卡，較舊的工單不能結束新的等待。
function reconcileTicketSnapshot(ticket: TicketDetail): void {
  // 從目前卡片取對話識別；工單本身不保存或回傳對話識別。
  const position = answerEntries.value.findIndex(
    // 重送較新的嘗試可能回傳更早的快照，必須同時比對兩個識別。
    (entry) =>
      entry.output.question_id === ticket.question_id &&
      entry.output.attempt_id === ticket.attempt_id,
  );
  const entry = answerEntries.value[position];
  if (!entry) {
    return;
  }

  // 已完成終態維持原畫面；執行中或不確定的狀態可由同次快照補上。
  if (
    entry.output.status !== "unconfirmed" &&
    entry.output.status !== "running"
  ) {
    return;
  }
  storeAnswerEntry(position, {
    question: entry.question,
    output: {
      ...entry.output,
      status: ticket.answer_status,
      answer: ticket.answer,
      citations: ticket.citations,
      search_results: ticket.search_results,
    },
  });

  // 只清除尚未另行編輯的同次草稿。
  if (
    admittedQuestionDraft &&
    sameAnswerAttempt(admittedQuestionDraft, entry.output)
  ) {
    admittedQuestionDraft = null;
    answerQuestion.value = "";
  }

  // 建單交易已保存停等；釋放操作後才取消傳輸，遲到終態不能覆寫快照。
  const operation = answerOperation.value;
  // prettier-ignore
  if (
    ticket.answer_status === "stopped"
    && operation?.admission
    && sameAnswerAttempt(operation.admission, entry.output)
  ) {
    answerOperation.value = null;
    operation.controller.abort();
  }
}

// 明確送出一次建單；未知結果保留草稿，只接受手動重送或列表讀回。
async function submitTicket(): Promise<void> {
  // 寫入只屬於原編輯器與登入；連按、關閉及前景切換不能發起第二次 POST。
  const owner = identity.value;
  const editor = ticketEditor.value;
  // prettier-ignore
  if (
    !owner
    || !editor
    || !isServiceReady.value
    || ticketWrite.value
  ) {
    return;
  }

  // 使用者修正或重試前保留實際文字，不去除原本的空白。
  const description = ticketDescription.value;
  ticketFormError.value = validateTicketText(description, "問題描述");
  ticketDescriptionInvalid.value = !!ticketFormError.value;
  if (ticketDescriptionInvalid.value) {
    focus(ticketDescriptionInput);
    return;
  }

  // 發送的本文只有後端約定的三個欄位。
  const operation: Operation = {
    controller: new AbortController(),
    foregroundEpoch: null,
  };
  ticketWrite.value = operation;
  try {
    const result = await requestApi("/api/tickets", operation, {
      method: "POST",
      body: {
        question_id: editor.source.question_id,
        attempt_id: editor.source.attempt_id,
        description,
      },
      headers: { "X-CSRF-Protection": "1" },
    });
    if (!isCurrentTicket(operation, owner, ticketWrite.value)) {
      return;
    }

    // 未登入立即隱藏所有私人資料；503 只讀回服務啟用狀態。
    if (result.status === 401) {
      expireWorkbenchSession("登入工作階段已失效，請重新登入後查看工單。");
      return;
    }
    if (result.status === 503) {
      await recheckBusinessSetup();
      if (!isCurrentTicket(operation, owner, ticketWrite.value)) {
        return;
      }
    }

    // 原題識別必須相符；同題不同嘗試的重送可能合法讀回既有工單。
    const saved =
      result.status === 200
        ? readTicketDetail(result.data, owner.username)
        : null;
    if (saved && saved.question_id === editor.source.question_id) {
      reconcileTicketSnapshot(saved);
      resetTicketEditor();
      ticketDetailId.value = saved.id;
      ticketDetail.value = saved;
      ticketOffset.value = 0;
      ticketCreated.value = true;
      ticketView.value = "detail";
      focus(ticketHeading);
      return;
    }

    // 寫入未獲確認或被拒絕時保留草稿，讓使用者能手動復原。
    ticketFormError.value = ticketFailureMessage(result.status, true);
    if (ticketFormError.value === TICKET_WRITE_UNCONFIRMED) {
      // 只記錄實際送出且結果未知的描述；欄位或 HTTP 拒絕不能建立復原標記。
      unconfirmedTicketDescription = description;
    }
    ticketDescriptionInvalid.value = result.status === 422;
    focus(
      ticketDescriptionInvalid.value
        ? ticketDescriptionInput
        : ticketFormFeedback,
    );
  } finally {
    // 較舊的寫入不能清除新工作階段的提交狀態。
    if (ticketWrite.value === operation) {
      ticketWrite.value = null;
    }
  }
}

// 更正結案欄位只解除欄位錯誤，確認狀態的回饋由讀取結果決定。
function handleTicketResolutionInput(): void {
  if (ticketResolutionInvalid.value) {
    ticketResolutionInvalid.value = false;
    ticketCloseFeedback.value = "";
  }
}

// 結案送出和讀回共用明確的登入、權限與資源失效處理。
function handleTicketCloseAccess(status: number | null): boolean {
  // 無法再確認原帳號可操作時，撤銷舊身分的私有畫面和草稿。
  if (status === 401 || status === 403) {
    expireWorkbenchSession(
      status === 401
        ? "登入工作階段已失效，請重新登入後查看工單。"
        : "無法驗證客服權限或要求來源，請重新登入後查看工單。",
    );
    return false;
  }

  // 目標已不可見，只保留安全的讀取錯誤與既有重查入口。
  if (status === 404) {
    resetTicketResolution();
    ticketDetail.value = null;
    ticketReadError.value = ticketFailureMessage(status, false);
    return false;
  }
  return true;
}

// 提交或讀回已確認結案後，一併替換快照、清除草稿並顯示各自的回饋。
function publishClosedTicket(ticket: TicketDetail, message: string): void {
  resetTicketResolution();
  ticketDetail.value = ticket;
  ticketCloseFeedback.value = message;
}

// 客服明確送出一次結案，未知結果及競爭衝突僅能讀回原工單。
async function settleTicketClose(checkOnly = false): Promise<void> {
  // 讀回與提交都鎖定當前開啟中的詳情、客服身分及尚未占用的寫入權。
  const owner = identity.value;
  const detail = ticketDetail.value;
  // prettier-ignore
  if (
    owner?.role !== "support"
    || !isServiceReady.value
    || ticketView.value !== "detail"
    || detail?.status !== "open"
    || ticketDetailId.value !== detail.id
    || ticketWrite.value
    || checkOnly !== (ticketCloseRecovery.value !== null)
  ) {
    return;
  }
  const id = detail.id;
  const path = `/api/tickets/${id}`;
  const resolution = ticketResolution.value;

  // 僅提交時驗證草稿；確認結果只查詢工單，不編輯原文也不重送。
  if (!checkOnly) {
    ticketCloseFeedback.value = validateTicketText(resolution, "處理結果");
    ticketResolutionInvalid.value = !!ticketCloseFeedback.value;
    if (ticketResolutionInvalid.value) {
      ticketResolutionInput.value?.focus();
      return;
    }
  }

  // 同步占用寫入權，阻擋連按並讓同一次取消權涵蓋後續讀回。
  const operation: Operation = {
    controller: new AbortController(),
    foregroundEpoch: null,
  };
  ticketWrite.value = operation;
  ticketCloseFeedback.value = "";
  // 每次等待後都驗證原畫面、工單和工作階段，避免遲到結果或焦點跨頁。
  const ownsClose = () =>
    isCurrentTicket(operation, owner, ticketWrite.value) &&
    ticketView.value === "detail" &&
    ticketDetailId.value === id;
  try {
    if (!checkOnly) {
      // 異動本文只有原樣的處理結果，與其他表單沿用同源及 CSRF 防護。
      const result = await requestApi(`${path}/close`, operation, {
        method: "POST",
        body: { resolution },
        headers: { "X-CSRF-Protection": "1" },
      });
      if (!ownsClose()) {
        return;
      }

      // 確認拒絕時使用既有登入及讀取復原流程，避免展示失效的資料。
      if (!handleTicketCloseAccess(result.status)) {
        return;
      }
      // 欄位被後端明確拒絕時可直接更正，不需要查詢結案結果。
      if (result.status === 422) {
        ticketResolutionInvalid.value = true;
        ticketCloseFeedback.value = "處理結果未通過格式檢查，請修正後再送出。";
        return;
      }

      // 只採用此次工單已保存的結果；其餘回應都先查詢持久狀態。
      const saved =
        result.status === 200
          ? readTicketDetail(result.data, owner.username, "visible")
          : null;
      // prettier-ignore
      if (
        saved?.id === id
        && saved.status === "closed"
        && saved.resolution === resolution
      ) {
        publishClosedTicket(saved, "已結案，處理結果已儲存。");
        return;
      }
      // 409 已確認結案；其他未確認回應仍可能已提交，都要讀回有效結果。
      ticketCloseRecovery.value =
        result.status === 409 ? "conflict" : "unknown";
      if (result.status === 503) {
        // 服務仍可用才讀工單；失去啟用或登入會撤銷目前畫面的更新權。
        await recheckBusinessSetup();
        if (!ownsClose()) {
          return;
        }
      }
    }

    // 無法從異動回應判斷結果時，只讀原工單一次。
    const result = await requestApi(path, operation, { method: "GET" });
    if (!ownsClose() || !handleTicketCloseAccess(result.status)) {
      return;
    }

    // 讀回的 503 也可能是啟用失效；仍啟用才保留草稿及手動確認入口。
    if (result.status === 503) {
      await recheckBusinessSetup();
      if (!ownsClose()) {
        return;
      }
    }

    // 已結案永遠顯示資料庫中的勝出結果，不以本次草稿覆蓋。
    const saved =
      result.status === 200
        ? readTicketDetail(result.data, owner.username, "visible")
        : null;
    const conflict = ticketCloseRecovery.value === "conflict";
    if (saved?.id === id && saved.status === "closed") {
      publishClosedTicket(
        saved,
        conflict
          ? "這張工單已經結案，以下顯示已儲存的處理結果。"
          : "已確認這張工單結案，以下顯示已儲存的處理結果。",
      );
      return;
    }

    // 只有確定工單仍開啟且未收到結案衝突，才恢復手動提交。
    if (saved?.id === id && saved.status === "open" && !conflict) {
      ticketDetail.value = saved;
      ticketCloseRecovery.value = null;
      ticketCloseFeedback.value =
        "工單仍開啟。已保留處理結果，您可以再次手動結案。";
      return;
    }
    ticketCloseFeedback.value = conflict
      ? "工單已結案，目前無法讀取處理結果。已保留草稿，請再次確認結案結果。"
      : "目前無法確認結案結果。已保留草稿，請先確認結案結果。";
  } finally {
    // 畫面完成更新後才聚焦，並在交還操作權前再次核對擁有者。
    if (ownsClose()) {
      await nextTick();
      if (ownsClose()) {
        const feedbackTarget = ticketResolutionInvalid.value
          ? ticketResolutionInput.value
          : (ticketReadFeedback.value ?? ticketCloseFeedbackElement.value);
        feedbackTarget?.focus();
      }
    }
    if (ticketWrite.value === operation) {
      ticketWrite.value = null;
    }
  }
}

// 清單或詳情使用獨立的讀取擁有權，切換時取消舊請求及清空舊資料。
async function loadTickets(
  id: string | null,
  offset = ticketOffset.value,
): Promise<void> {
  // 寫入中或結案結果待確認時暫停導航；明確切換時會清除結案草稿，保留建單草稿。
  const owner = identity.value;
  // prettier-ignore
  if (
    !owner
    || !isServiceReady.value
    || isTicketNavigationLocked.value
  ) {
    return;
  }

  // 清除前一次結果，防止切換詳情或分頁時顯示舊資料。
  ticketRead.value?.controller.abort();
  const operation: Operation = {
    controller: new AbortController(),
    foregroundEpoch: null,
  };
  ticketRead.value = operation;
  resetTicketResolution();
  ticketOffset.value = offset;
  ticketDetailId.value = id;
  ticketDetail.value = null;
  ticketPage.value = null;
  ticketReadError.value = "";
  ticketCreated.value = false;
  const view = id ? "detail" : "list";
  ticketView.value = view;

  // 切換分頁或詳情時，前一次讀取不能更新目前內容或搶走焦點。
  // prettier-ignore
  const ownsView = () =>
    ticketView.value === view
    && ticketDetailId.value === id
    && ticketOffset.value === offset;
  // 只接受同一個讀取操作且登入身分、服務和畫面仍有效的結果。
  const ownsRead = () =>
    isCurrentTicket(operation, owner, ticketRead.value) && ownsView();
  focus(ticketHeading, ownsRead);

  try {
    // 列表只讀指定的一頁；明細依已驗證的工單識別定位。
    const path = id
      ? `/api/tickets/${id}`
      : `/api/tickets?limit=${TICKET_PAGE_SIZE}&offset=${offset}`;
    const result = await requestApi(path, operation, { method: "GET" });
    if (!ownsRead()) {
      return;
    }

    // 失去登入或啟用後回到既有安全流程，不顯示任何舊資料。
    if (result.status === 401) {
      expireWorkbenchSession("登入工作階段已失效，請重新登入後查看工單。");
      return;
    }
    if (result.status === 503) {
      await recheckBusinessSetup();
      if (!ownsRead()) {
        return;
      }
    }

    // 僅採用合法分頁，或符合本次識別與登入者的快照。
    if (result.status === 200) {
      if (id) {
        const detail = readTicketDetail(result.data, owner.username, "visible");
        if (detail?.id === id) {
          // 跨帳號快照只供檢視，本人快照才可確認同一次問答或建單。
          if (detail.created_by === owner.username) {
            reconcileTicketSnapshot(detail);
            const editor = ticketEditor.value;
            const submittedDescription = unconfirmedTicketDescription;
            // prettier-ignore
            if (
              submittedDescription !== null
              && editor?.source.question_id === detail.question_id
              && editor.source.attempt_id === detail.attempt_id
            ) {
              unconfirmedTicketDescription = null;

              // 只收起已保存且未再編輯的草稿，保留後續的編輯及拒絕回饋。
              if (ticketFormError.value === TICKET_WRITE_UNCONFIRMED) {
                if (
                  ticketDescription.value === submittedDescription &&
                  detail.description === submittedDescription
                ) {
                  resetTicketEditor();
                } else {
                  ticketFormError.value = "";
                }
              }
            }
          }
          ticketDetail.value = detail;
          return;
        }
      } else {
        const page = readTicketPage(result.data, TICKET_PAGE_SIZE);
        if (page) {
          ticketPage.value = page;
          return;
        }
      }
    }

    // 原導航仍有效時才顯示讀取失敗與手動重試。
    ticketReadError.value = ticketFailureMessage(result.status, false);
    focus(
      ticketReadFeedback,
      // 讀取結束才顯示錯誤；若期間有新的導航或換帳號，焦點留在新畫面。
      // prettier-ignore
      () =>
        ticketRead.value === null
        && identity.value === owner
        && isServiceReady.value
        && ownsView(),
    );
  } finally {
    // 只釋放這次讀取；返回工作台或換頁不等待舊讀取完成。
    if (ticketRead.value === operation) {
      ticketRead.value = null;
    }
  }
}

// 回到問答及搜尋，保留本頁對話與尚待確認的建單描述。
function showWorkbench(): void {
  if (isTicketNavigationLocked.value || ticketView.value === "workbench") {
    return;
  }
  // 先釋放讀取擁有權，避免遲到錯誤把焦點帶回隱藏的工單區。
  const operation = ticketRead.value;
  ticketRead.value = null;
  operation?.controller.abort();
  ticketPage.value = null;
  ticketDetail.value = null;
  ticketDetailId.value = null;
  ticketReadError.value = "";
  resetTicketResolution();
  ticketView.value = "workbench";
  focus(ticketEditor.value ? ticketDescriptionInput : workbenchHeading);
}

// 登出失敗或逾時時只讀回 session，避免盲目重送非冪等異動。
async function submitLogout(): Promise<void> {
  // 由有效工作台發起登出；未確認的嘗試即使失去啟用仍允許手動重試。
  if (!logoutPending.value && (!isServiceReady.value || !identity.value)) {
    return;
  }
  const operation = beginOperation();
  if (!operation) {
    return;
  }

  // 登出讀回期間先隱藏舊身分，避免不確定狀態繼續展示私有資料。
  identity.value = null;
  authState.value = "checking";
  authMessage.value = "";

  // 登出是同源異動，帶 CSRF 標頭並只把 204 視為明確成功。
  const result = await requestApi("/api/logout", operation, {
    method: "POST",
    headers: { "X-CSRF-Protection": "1" },
  });
  if (activeOperation.value !== operation) {
    return;
  }
  if (result.status === 204) {
    // 登出不受啟用門檻限制；成功後重新讀取狀態再決定是否顯示登入表單。
    logoutPending.value = false;
    const setupResult = await recheckSetup(operation);
    if (activeOperation.value !== operation) {
      return;
    }
    // 204 已確認撤銷，啟用失效或查詢失敗也應顯示成功結果。
    if (setupResult === "ready") {
      authState.value = "signed-out";
    }
    authMessage.value = "已安全登出。";
    finishOperation(operation);
    focus(setupResult === "ready" ? usernameInput : setupLogoutFeedback);
    return;
  }

  // 未知結果只讀回一次；啟用檢查不能證明已登出，也不能清掉重試。
  logoutPending.value = true;
  await readSession(operation, {
    focusAuthenticated: true,
    focusUnauthenticated: true,
  });
}

// 帳號變更時清除密碼，避免將前一帳號的密碼送給新帳號。
watch(username, (next, previous) => {
  // 大小寫不影響示範帳號識別；其他名稱變更代表切換帳號。
  if (next.trim().toLowerCase() !== previous.trim().toLowerCase()) {
    password.value = "";
  }
});

// 服務失效、身分清除或切換時立即取消業務操作並清除私有內容。
watch(
  [isServiceReady, identity],
  ([ready, currentIdentity], previous) => {
    // 確認身分離開或更換時，避免共用瀏覽器帶入前一人的關鍵字。
    const identityChanged =
      previous[1] !== null && previous[1] !== currentIdentity;
    if (identityChanged) {
      knowledgeQuery.value = "";
    }

    // 同步撤銷舊身分的取消權，避免回應在下一次畫面更新前落地。
    // prettier-ignore
    if (
      !ready
      || !currentIdentity
      || identityChanged
    ) {
      cancelKnowledgeSearch();
      clearAnswer();
      clearTickets();
      businessSetupReadback?.controller.abort();
    }
  },
  { flush: "sync" },
);

// 靜態頁掛載後才讀取服務與登入狀態，保持 generate 不依賴後端。
onMounted(() => {
  // 初次狀態確認與手動恢復使用同一路徑，不呼叫模型或重試。
  void refreshSetupStatus();
  // 同時追蹤離開與返回，避免採用前一次停留的啟用狀態。
  window.addEventListener("blur", leaveForeground);
  window.addEventListener("focus", checkSetupOnForeground);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  // 瀏覽器可能快取離開的頁面；返回需確認啟用及身分，但不續聊。
  window.addEventListener("pagehide", handlePageHide);
  window.addEventListener("pageshow", handlePageShow);
});

// 離開頁面時取消目前操作，使遲到回應不能更新卸載後的畫面。
onBeforeUnmount(() => {
  // 停止接收焦點與可見性事件，避免卸載後開始新的查詢。
  window.removeEventListener("blur", leaveForeground);
  window.removeEventListener("focus", checkSetupOnForeground);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  window.removeEventListener("pagehide", handlePageHide);
  window.removeEventListener("pageshow", handlePageShow);

  // 工作台卸載時取消業務操作與回讀，避免遲到結果留在新畫面。
  clearPageBusinessState();

  // 先撤銷畫面更新權，再中止請求及其後續 readback。
  const operation = activeOperation.value;
  activeOperation.value = null;
  operation?.controller.abort();
});
</script>

<template>
  <header class="app-header">
    <div class="app-header-inner">
      <!-- 字印只作品牌識別，避免輔助技術將裝飾重複朗讀。 -->
      <p class="brand-mark">
        <span class="brand-seal" aria-hidden="true">臺</span>
        <span>
          <span class="brand-title">臺灣知識工作台</span>
          <span class="brand-subtitle">AI 客服工單與知識查詢</span>
        </span>
      </p>
      <div
        v-if="isServiceReady && authState === 'signed-in' && identity"
        class="header-account"
      >
        <span>{{ roleTitle }} · {{ identity.username }}</span>
        <button
          type="button"
          class="logout-action"
          :disabled="isAuthBusy"
          @click="submitLogout"
        >
          {{ isAuthActionBusy ? "登出中…" : "登出" }}
        </button>
      </div>
    </div>
  </header>
  <main v-if="!isServiceReady" class="setup-shell" aria-labelledby="page-title">
    <header class="page-heading">
      <h1 id="page-title">本機設定</h1>
      <p>確認本機服務啟用狀態，並依提示完成必要設定。</p>
    </header>

    <section class="setup-card" :aria-busy="isLoading">
      <div v-if="isLoading" class="status-content" role="status">
        <h2>正在確認服務狀態</h2>
        <p>正在讀取本機服務狀態。</p>
      </div>

      <div v-else-if="hasSetupError" class="status-content" role="alert">
        <h2 ref="setupErrorHeading" tabindex="-1">目前無法確認狀態</h2>
        <p>目前無法取得有效的服務狀態，請確認本機服務後手動重新確認。</p>
      </div>

      <div v-else-if="setupStatus" class="status-content" aria-live="polite">
        <h2 ref="setupStatusHeading" tabindex="-1">
          {{ statusTitles[setupStatus.status] }}
        </h2>
        <p>{{ setupStatus.message }}</p>
      </div>

      <div v-else class="status-content" role="status">
        <h2>尚未確認服務狀態</h2>
        <p>請由本機操作人員確認服務已啟動。</p>
      </div>

      <dl class="model-details">
        <dt>指定模型</dt>
        <dd>
          <code>{{ SETUP_MODEL }}</code>
        </dd>
      </dl>

      <section
        v-if="!setupStatus || setupStatus.status !== 'ready'"
        class="operator-guidance"
        aria-labelledby="operator-guidance-title"
      >
        <h2 id="operator-guidance-title">本機設定</h2>
        <p>請由本機操作人員在終端依序完成設定與驗證：</p>
        <ol>
          <li><code>./bin/local set-key</code></li>
          <li><code>./bin/local verify-key</code></li>
        </ol>
      </section>

      <p
        v-if="logoutPending || authMessage"
        ref="setupLogoutFeedback"
        :class="{ 'form-feedback': logoutPending }"
        :role="logoutPending ? 'alert' : 'status'"
        tabindex="-1"
      >
        {{ logoutPending ? LOGOUT_UNCONFIRMED : authMessage }}
      </p>

      <div class="refresh-row">
        <button
          v-if="logoutPending"
          type="button"
          :disabled="isAuthBusy"
          @click="submitLogout"
        >
          重試登出
        </button>
        <button
          type="button"
          :disabled="isLoading || isAuthBusy"
          @click="refreshSetupStatus(true)"
        >
          {{ isLoading ? "確認中…" : "重新確認狀態" }}
        </button>
      </div>
    </section>
  </main>

  <main
    v-else-if="authState !== 'signed-in'"
    class="auth-shell"
    aria-live="polite"
  >
    <section v-if="authState === 'checking'" class="auth-card" role="status">
      <p class="eyebrow">登入狀態</p>
      <h1>正在確認工作階段</h1>
      <p>正在向本機服務確認登入狀態。</p>
    </section>

    <section v-else-if="authState === 'error'" class="auth-card" role="alert">
      <p class="eyebrow">需要重新確認</p>
      <h1 ref="authFeedbackHeading" tabindex="-1">目前無法確認登入狀態</h1>
      <p>{{ authMessage }}</p>
      <div class="refresh-row">
        <button
          v-if="logoutPending"
          type="button"
          :disabled="isAuthBusy"
          @click="submitLogout"
        >
          重試登出
        </button>
        <button type="button" :disabled="isAuthBusy" @click="checkSession">
          {{ isAuthActionBusy ? "確認中…" : "重新確認登入狀態" }}
        </button>
      </div>
    </section>

    <section
      v-else-if="authState === 'signed-out'"
      class="auth-card login-card"
    >
      <h1 id="login-title">登入客服工作台</h1>
      <p class="login-intro">
        請使用本機安全提供的示範帳號。此頁不提供註冊或帳號管理。
      </p>

      <p
        v-if="authMessage"
        ref="loginFeedbackHeading"
        class="form-feedback"
        role="alert"
        tabindex="-1"
      >
        {{ authMessage }}
      </p>

      <form
        class="login-form"
        aria-labelledby="login-title"
        @submit.prevent="submitLogin"
      >
        <div class="field-group">
          <label for="username">示範帳號</label>
          <input
            id="username"
            ref="usernameInput"
            v-model="username"
            name="username"
            type="text"
            autocomplete="username"
            maxlength="64"
            required
            :disabled="isAuthActionBusy"
          />
        </div>

        <div class="field-group">
          <label for="password">密碼</label>
          <input
            id="password"
            ref="passwordInput"
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
            maxlength="128"
            required
            :disabled="isAuthActionBusy"
          />
        </div>

        <button type="submit" class="primary-action" :disabled="isAuthBusy">
          {{ isAuthActionBusy ? "登入中…" : "登入" }}
        </button>
      </form>

      <p class="login-note">登入資訊只會送往本機服務驗證。</p>
    </section>
  </main>

  <main v-else class="workbench-page" aria-live="polite">
    <section
      v-if="identity"
      class="workbench-shell"
      :aria-label="`${roleTitle}工作台`"
    >
      <nav class="app-nav" aria-label="工作台導覽">
        <div class="workbench-nav">
          <button
            type="button"
            class="workbench-nav-action"
            :aria-current="ticketView === 'workbench' ? 'page' : undefined"
            :disabled="isTicketNavigationLocked"
            @click="showWorkbench"
          >
            工作台
          </button>
          <button
            type="button"
            class="workbench-nav-action"
            :aria-current="ticketView !== 'workbench' ? 'page' : undefined"
            :disabled="isTicketNavigationLocked"
            @click="loadTickets(null, 0)"
          >
            {{ ticketListTitle }}
          </button>
        </div>
      </nav>

      <div class="workbench-main">
        <header class="workbench-heading" v-show="ticketView === 'workbench'">
          <h1 ref="workbenchHeading" tabindex="-1">提出問題</h1>
          <p class="knowledge-search-help" role="status">
            本段對話已確認 {{ answerEntries.length }} /
            {{ MAX_CONVERSATION_QUESTIONS }} 題；重試不增加題數。
          </p>
        </header>

        <p v-if="authMessage" class="workbench-feedback" role="status">
          {{ authMessage }}
        </p>

        <section
          v-show="ticketView === 'workbench'"
          ref="answerWorkspace"
          class="answer-workspace"
          aria-labelledby="answer-title"
        >
          <h2 id="answer-title" class="visually-hidden">問答工作區</h2>
          <p class="knowledge-search-intro">
            以本機知識文件尋找依據。您可以在本頁追問，或手動重試失敗的問題。請勿輸入個人或敏感資訊；離開頁面後無法續聊。
          </p>

          <section
            v-if="answerNotice || isAnswerAtLimit"
            class="knowledge-search-feedback"
            aria-labelledby="answer-notice-title"
            role="alert"
          >
            <h3 id="answer-notice-title" tabindex="-1">對話提示</h3>
            <p v-if="answerNotice">{{ answerNotice.message }}</p>
            <p v-if="isAnswerAtLimit && answerNotice?.block !== 'new-question'">
              {{ ANSWER_LIMIT_MESSAGE }}
            </p>
          </section>

          <!-- 每題各自保留問題與來源欄，避免追問時混用前題證據。 -->
          <ol
            v-if="answerEntries.length"
            class="answer-history"
            aria-label="本段對話問答"
          >
            <li
              v-for="(entry, position) in answerEntries"
              :key="entry.output.question_id"
              class="answer-entry"
            >
              <div class="answer-transcript">
                <h3 :id="`question-${position}`">第 {{ position + 1 }} 題</h3>
                <p class="answer-text">{{ entry.question }}</p>
                <p v-if="entry.output.status === 'running'" role="status">
                  {{
                    answerOperation?.retry?.output.question_id ===
                    entry.output.question_id
                      ? "正在重試這一題，請稍候。"
                      : "正在查詢知識文件並處理這一題，請稍候。"
                  }}
                </p>
                <template v-else-if="entry.output.status === 'answered'">
                  <h4 :id="`answer-result-${position}`" tabindex="-1">回答</h4>
                  <p class="knowledge-search-help">
                    模型 <code>{{ SETUP_MODEL }}</code>
                  </p>
                  <AnswerText :text="entry.output.answer ?? ''" />
                </template>
                <template v-else>
                  <!-- 資料不足保留搜尋與建單入口，以標題及色面區別技術失敗。 -->
                  <div
                    class="knowledge-search-feedback"
                    :class="{
                      'is-insufficient':
                        entry.output.status === 'insufficient_evidence',
                    }"
                  >
                    <h4 :id="`answer-result-${position}`" tabindex="-1">
                      {{ answerStatusTitles[entry.output.status] }}
                    </h4>
                    <p v-if="entry.output.status === 'unconfirmed'">
                      伺服器已受理，但目前無法確認回答結果。可從這一題建立工單，取得已保存的證據。
                    </p>
                    <p v-else-if="entry.output.status === 'stopped'">
                      這次問答已停止等待，可查看建單時保存的搜尋結果。
                    </p>
                    <p v-else>
                      {{ answerStatusMessages[entry.output.status] }}
                    </p>
                    <p
                      v-if="entry.notice && answerOperation?.retry !== entry"
                      :id="`answer-retry-notice-${position}`"
                      role="alert"
                      tabindex="-1"
                    >
                      {{ entry.notice.message }}
                    </p>
                  </div>
                  <p v-if="answerOperation?.retry === entry" role="status">
                    正在重試這一題，請稍候。
                  </p>
                  <button
                    v-if="
                      !isAnswerConversationBlocked &&
                      entry.notice?.block !== 'retry' &&
                      entry.output.status !== 'unconfirmed'
                    "
                    type="button"
                    class="answer-secondary-action"
                    :aria-label="`重試這一題（第 ${position + 1} 題）`"
                    :disabled="
                      answerOperation !== null || ticketEditor !== null
                    "
                    @click="submitAnswer(entry)"
                  >
                    重試這一題
                  </button>
                </template>
                <button
                  :id="`create-ticket-${entry.output.question_id}`"
                  type="button"
                  class="answer-secondary-action"
                  :aria-label="`建立工單（第 ${position + 1} 題）`"
                  :disabled="
                    ticketEditor !== null || answerOperation?.retry === entry
                  "
                  @click="openTicketEditor(entry)"
                >
                  建立工單
                </button>
              </div>
              <aside
                class="answer-evidence"
                :aria-labelledby="`evidence-${position} question-${position}`"
              >
                <h4 :id="`evidence-${position}`">
                  第 {{ position + 1 }} 題 ·
                  {{
                    entry.output.status === "answered" ? "引用來源" : "搜尋依據"
                  }}
                </h4>
                <KnowledgeResultList
                  v-if="entry.output.status === 'answered'"
                  :results="entry.output.citations"
                />
                <template
                  v-else-if="
                    entry.output.status !== 'running' &&
                    entry.output.status !== 'unconfirmed'
                  "
                >
                  <p class="knowledge-search-help">本次已取得的搜尋結果</p>
                  <KnowledgeResultList
                    v-if="entry.output.search_results.length"
                    :results="entry.output.search_results"
                  />
                  <p v-else>本次問答沒有取得符合的知識片段。</p>
                </template>
                <p v-else class="knowledge-search-help">
                  {{
                    entry.output.status === "running"
                      ? "回答完成後會顯示這一題的依據。"
                      : "可從這一題建立工單，取得已保存的證據。"
                  }}
                </p>
              </aside>
            </li>
          </ol>

          <section
            v-if="ticketEditor"
            class="ticket-editor"
            aria-labelledby="ticket-editor-title"
          >
            <h3 id="ticket-editor-title">建立工單</h3>
            <p class="knowledge-search-help">
              原始問題會另存為證據，您可以補充問題背景。
            </p>
            <p class="answer-text">
              <strong>原始問題：</strong>{{ ticketEditor.question }}
            </p>
            <form
              class="login-form answer-form"
              :aria-busy="ticketWrite !== null"
              novalidate
              @submit.prevent="submitTicket"
            >
              <div class="field-group">
                <label for="ticket-description">問題描述</label>
                <textarea
                  id="ticket-description"
                  ref="ticketDescriptionInput"
                  v-model="ticketDescription"
                  name="description"
                  rows="5"
                  required
                  autocomplete="off"
                  :readonly="ticketWrite !== null"
                  :aria-invalid="ticketDescriptionInvalid ? 'true' : undefined"
                  :aria-describedby="
                    ticketFormError
                      ? 'ticket-description-help ticket-form-error'
                      : 'ticket-description-help'
                  "
                  @input="handleTicketDescriptionInput"
                ></textarea>
                <p id="ticket-description-help" class="knowledge-search-help">
                  最多 4,000 個 Unicode 字元。
                </p>
                <p
                  v-if="ticketFormError"
                  id="ticket-form-error"
                  ref="ticketFormFeedback"
                  class="form-feedback"
                  role="alert"
                  tabindex="-1"
                >
                  {{ ticketFormError }}
                </p>
              </div>
              <button
                type="submit"
                class="primary-action"
                :disabled="ticketWrite !== null"
              >
                {{ ticketWrite ? "建立中…" : "送出工單" }}
              </button>
              <div class="ticket-actions">
                <button
                  type="button"
                  class="answer-secondary-action"
                  :disabled="ticketWrite !== null"
                  @click="closeTicketEditor"
                >
                  取消建單
                </button>
                <button
                  v-if="ticketFormError && !ticketDescriptionInvalid"
                  type="button"
                  class="answer-secondary-action"
                  :disabled="ticketWrite !== null"
                  @click="loadTickets(null, 0)"
                >
                  查看{{ ticketListTitle }}
                </button>
              </div>
            </form>
          </section>

          <div class="answer-tools">
            <form
              class="login-form answer-form answer-composer"
              aria-labelledby="answer-title"
              :aria-busy="answerOperation !== null"
              novalidate
              @submit.prevent="submitAnswer()"
            >
              <div class="field-group">
                <label for="answer-question">{{
                  answerEntries.length ? "追問" : "您的問題"
                }}</label>
                <textarea
                  id="answer-question"
                  ref="answerQuestionInput"
                  v-model="answerQuestion"
                  name="question"
                  rows="3"
                  required
                  autocomplete="off"
                  :readonly="
                    answerOperation !== null && answerOperation.retry === null
                  "
                  :aria-invalid="answerQuestionError ? 'true' : undefined"
                  :aria-describedby="
                    answerQuestionError
                      ? 'answer-question-help answer-question-error'
                      : 'answer-question-help'
                  "
                  @input="handleAnswerQuestionInput"
                ></textarea>
                <p id="answer-question-help" class="knowledge-search-help">
                  最多 2,000 個 Unicode 字元。
                </p>
                <p
                  v-if="answerQuestionError"
                  id="answer-question-error"
                  class="form-feedback"
                  role="alert"
                >
                  {{ answerQuestionError }}
                </p>
              </div>
              <button
                type="submit"
                class="primary-action"
                :disabled="
                  answerOperation !== null ||
                  ticketEditor !== null ||
                  isAnswerAtLimit ||
                  isAnswerConversationBlocked
                "
              >
                {{ answerEntries.length ? "送出追問" : "提出問題" }}
              </button>
              <button
                v-if="answerEntries.length || isAnswerConversationBlocked"
                type="button"
                class="answer-secondary-action"
                :disabled="answerOperation !== null || ticketEditor !== null"
                @click="startNewConversation"
              >
                開始新的對話
              </button>
            </form>

            <section
              v-show="ticketView === 'workbench'"
              ref="knowledgeSearchWorkspace"
              class="knowledge-search"
              aria-labelledby="knowledge-search-title"
            >
              <h2 id="knowledge-search-title">搜尋知識文件</h2>
              <p class="knowledge-search-intro">
                搜尋已匯入的真實文件片段，顯示原文件摘錄。
              </p>

              <form
                class="knowledge-search-form login-form"
                :aria-busy="knowledgeSearchState === 'loading'"
                @submit.prevent="submitKnowledgeSearch"
              >
                <div class="field-group">
                  <label for="knowledge-query">搜尋關鍵字</label>
                  <input
                    id="knowledge-query"
                    ref="knowledgeQueryInput"
                    v-model="knowledgeQuery"
                    name="query"
                    type="search"
                    required
                    :aria-invalid="knowledgeQueryError ? 'true' : undefined"
                    :aria-describedby="
                      knowledgeQueryError
                        ? 'knowledge-query-help knowledge-query-error'
                        : 'knowledge-query-help'
                    "
                    @input="handleKnowledgeQueryInput"
                  />
                  <p id="knowledge-query-help" class="knowledge-search-help">
                    最多 2,000 個 Unicode
                    字元。支援中文關鍵詞與自然問句，臺／台與全形字會正規化。
                  </p>
                  <p
                    v-if="knowledgeQueryError"
                    id="knowledge-query-error"
                    class="form-feedback"
                    role="alert"
                  >
                    {{ knowledgeQueryError }}
                  </p>
                </div>
                <button
                  type="submit"
                  class="primary-action"
                  :disabled="knowledgeSearchState === 'loading'"
                >
                  {{
                    knowledgeSearchState === "loading"
                      ? "搜尋中…"
                      : knowledgeSearchState === "error"
                        ? "重試搜尋"
                        : "搜尋知識"
                  }}
                </button>
              </form>

              <div
                v-if="knowledgeSearchState === 'loading'"
                class="knowledge-search-state"
                role="status"
              >
                <h3>正在搜尋知識</h3>
                <p>正在查詢本機知識文件，請稍候。</p>
              </div>

              <section
                v-else-if="knowledgeSearchState === 'error'"
                class="knowledge-search-feedback"
                role="alert"
              >
                <h3 ref="knowledgeSearchErrorHeading" tabindex="-1">
                  搜尋未完成
                </h3>
                <p>{{ knowledgeSearchError }}</p>
              </section>

              <section
                v-else-if="['empty', 'success'].includes(knowledgeSearchState)"
                class="knowledge-results"
                aria-labelledby="knowledge-results-title"
              >
                <h3
                  id="knowledge-results-title"
                  ref="knowledgeResultsHeading"
                  tabindex="-1"
                >
                  搜尋結果
                </h3>
                <p v-if="knowledgeSearchState === 'empty'" role="status">
                  查無「{{ knowledgeQuery }}」的符合片段，可修改關鍵字後重試。
                </p>
                <template v-else>
                  <p role="status">
                    找到 {{ knowledgeResults.length }} 筆知識片段。
                  </p>
                  <KnowledgeResultList :results="knowledgeResults" />
                </template>
              </section>
            </section>
          </div>
          <p
            v-if="
              answerOperation &&
              !answerOperation.retry &&
              !answerOperation.admission
            "
            id="answer-pending-status"
            class="knowledge-search-state"
            role="status"
          >
            正在查詢知識文件並處理第
            {{ answerEntries.length + 1 }} 題，請稍候。
          </p>
        </section>

        <section
          v-if="ticketView !== 'workbench'"
          class="ticket-workspace"
          aria-labelledby="ticket-title"
        >
          <h1 id="ticket-title" ref="ticketHeading" tabindex="-1">
            {{ ticketView === "list" ? ticketListTitle : "工單詳情" }}
          </h1>
          <button
            v-if="ticketView === 'detail'"
            type="button"
            class="answer-secondary-action"
            :disabled="isTicketNavigationLocked"
            @click="loadTickets(null)"
          >
            返回{{ ticketListTitle }}
          </button>

          <p v-if="ticketRead" class="knowledge-search-state" role="status">
            正在讀取工單，請稍候。
          </p>
          <div v-else-if="ticketReadError" class="knowledge-search-feedback">
            <p ref="ticketReadFeedback" role="alert" tabindex="-1">
              {{ ticketReadError }}
            </p>
            <button
              type="button"
              class="answer-secondary-action"
              :disabled="isTicketNavigationLocked"
              @click="loadTickets(ticketDetailId)"
            >
              重試讀取
            </button>
          </div>

          <template v-else-if="ticketPage">
            <p
              v-if="!ticketPage.tickets.length"
              class="knowledge-search-state"
              role="status"
            >
              {{ ticketOffset > 0 ? "這一頁沒有工單。" : "目前沒有工單。" }}
              <template v-if="ticketOffset === 0 && identity.role === 'user'">
                您可以從問答卡建立工單。
              </template>
            </p>
            <ul
              v-else
              class="ticket-list"
              :aria-label="`${ticketListTitle}列表`"
            >
              <li
                v-for="ticket in ticketPage.tickets"
                :key="ticket.id"
                class="ticket-list-row"
              >
                <div class="ticket-summary">
                  <button
                    type="button"
                    class="ticket-summary-link"
                    :aria-label="`查看工單：${ticket.description}（${ticket.id}）`"
                    @click="loadTickets(ticket.id)"
                  >
                    <span class="ticket-summary-text answer-text">{{
                      ticket.description
                    }}</span>
                  </button>
                  <p class="ticket-summary-preview answer-text">
                    <span class="visually-hidden">原始問題：</span
                    >{{ ticket.question.trimStart() }}
                  </p>
                  <p
                    class="ticket-summary-preview answer-text"
                    v-if="ticket.status === 'closed'"
                  >
                    <span>處理結果：</span>{{ ticket.resolution?.trimStart() }}
                  </p>
                  <code>{{ ticket.id }}</code>
                </div>
                <div class="ticket-times">
                  <time :datetime="ticket.created_at"
                    ><span class="visually-hidden">建立時間：</span
                    >{{ ticketTime(ticket.created_at) }}</time
                  >
                  <p v-if="ticket.closed_at">
                    結案時間：<time :datetime="ticket.closed_at">{{
                      ticketTime(ticket.closed_at)
                    }}</time>
                  </p>
                </div>
                <span
                  class="ticket-status"
                  :class="{ 'is-closed': ticket.status === 'closed' }"
                  >{{ ticket.status === "closed" ? "已結案" : "開啟" }}</span
                >
              </li>
            </ul>
            <nav
              v-if="ticketOffset > 0 || ticketPage.has_more"
              class="ticket-actions"
              aria-label="工單分頁"
            >
              <button
                v-if="ticketOffset > 0"
                type="button"
                class="answer-secondary-action"
                @click="loadTickets(null, ticketOffset - TICKET_PAGE_SIZE)"
              >
                上一頁
              </button>
              <button
                v-if="ticketPage.has_more"
                type="button"
                class="answer-secondary-action"
                @click="loadTickets(null, ticketOffset + TICKET_PAGE_SIZE)"
              >
                下一頁
              </button>
            </nav>
          </template>

          <template v-else-if="ticketDetail">
            <p
              v-if="ticketCreated"
              class="knowledge-search-state"
              role="status"
            >
              已儲存工單；同一問題再次送出會保留原工單和原描述。
            </p>
            <TicketSnapshot
              :ticket="ticketDetail"
              :close-recovery="ticketCloseRecovery"
            >
              <p
                v-if="ticketCloseFeedback"
                id="ticket-close-feedback"
                ref="ticketCloseFeedbackElement"
                :class="
                  ticketResolutionInvalid || ticketCloseRecovery
                    ? 'knowledge-search-feedback'
                    : 'knowledge-search-state'
                "
                :role="
                  ticketResolutionInvalid || ticketCloseRecovery
                    ? 'alert'
                    : 'status'
                "
                tabindex="-1"
              >
                {{ ticketCloseFeedback }}
              </p>
              <p
                v-else-if="ticketWrite"
                class="knowledge-search-state"
                role="status"
              >
                正在確認結案結果，請稍候。
              </p>
              <form
                v-if="
                  identity.role === 'support' && ticketDetail.status === 'open'
                "
                class="login-form answer-form"
                aria-label="工單結案"
                novalidate
                :aria-busy="ticketWrite !== null"
                @submit.prevent="settleTicketClose()"
              >
                <div
                  v-if="ticketCloseRecovery !== 'conflict'"
                  class="field-group"
                >
                  <label for="ticket-resolution">處理結果</label>
                  <textarea
                    id="ticket-resolution"
                    ref="ticketResolutionInput"
                    v-model="ticketResolution"
                    name="resolution"
                    rows="5"
                    required
                    autocomplete="off"
                    :readonly="isTicketNavigationLocked"
                    :aria-invalid="ticketResolutionInvalid ? 'true' : undefined"
                    :aria-describedby="
                      ticketCloseFeedback
                        ? 'ticket-resolution-help ticket-close-feedback'
                        : 'ticket-resolution-help'
                    "
                    @input="handleTicketResolutionInput"
                  ></textarea>
                  <p id="ticket-resolution-help" class="knowledge-search-help">
                    最多 4,000 個 Unicode
                    字元。結案後無法修改處理結果或重新開啟工單。
                  </p>
                </div>
                <button
                  v-if="ticketCloseRecovery"
                  type="button"
                  class="answer-secondary-action"
                  :disabled="ticketWrite !== null"
                  @click="settleTicketClose(true)"
                >
                  確認結案結果
                </button>
                <button
                  v-else
                  type="submit"
                  class="primary-action"
                  :disabled="ticketWrite !== null"
                >
                  結案
                </button>
              </form>
            </TicketSnapshot>
          </template>
        </section>

        <p class="workbench-note">
          {{ roleWorkspace }} ·
          {{ roleDescription }}
          登入狀態由本機服務管理；重新載入頁面時會重新確認目前角色。
        </p>
      </div>
    </section>
  </main>
</template>
