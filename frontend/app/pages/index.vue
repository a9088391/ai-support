<script setup lang="ts">
const SETUP_MODEL = "gemini-3.5-flash-lite" as const;
const REQUEST_TIMEOUT_MS = 10_000;
const LOGOUT_UNCONFIRMED = "目前無法確認登出結果，請手動重試登出。";
const setupStatuses = [
  "unconfigured",
  "unverified",
  "ready",
  "invalid",
] as const;

type SetupStatus = (typeof setupStatuses)[number];
type Role = "user" | "support";
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

interface Operation {
  readonly controller: AbortController;
  readonly foregroundEpoch: number | null;
}

interface ApiResult {
  readonly status: number | null;
  readonly data: unknown;
  readonly retryAfter: string | null;
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
const identity = ref<IdentityDto | null>(null);
const username = ref("");
const password = ref("");
const usernameInput = ref<HTMLInputElement | null>(null);
const passwordInput = ref<HTMLInputElement | null>(null);
const setupStatusHeading = ref<HTMLElement | null>(null);
const setupErrorHeading = ref<HTMLElement | null>(null);
const setupLogoutFeedback = ref<HTMLElement | null>(null);
const authFeedbackHeading = ref<HTMLElement | null>(null);
const workbenchHeading = ref<HTMLElement | null>(null);
const loginFeedbackHeading = ref<HTMLElement | null>(null);

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
// 工作台標題直接依伺服器回傳角色顯示，不建立額外導航狀態。
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
// 空狀態依角色使用不同文案，清楚表示目前沒有業務項目。
const emptyStateDescription = computed(() =>
  identity.value?.role === "user"
    ? "目前沒有可顯示的個人服務項目。此版本先提供登入狀態與角色識別。"
    : "目前沒有可顯示的客服處理項目。此版本先提供登入狀態與客服角色識別。",
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
  }, REQUEST_TIMEOUT_MS);

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
function focus(target: { readonly value: HTMLElement | null }): void {
  // 等待目標元素掛載後再移動鍵盤焦點。
  void nextTick(() => target.value?.focus());
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

  // 啟用狀態只讀一次；失敗交由使用者手動恢復。
  const result = await requestApi("/api/setup", operation, { method: "GET" });
  if (activeOperation.value !== operation) {
    return "stale";
  }
  // 離開前景前開始的查詢已過期，即使回應有效也不能更新畫面。
  if (isForegroundCheck && operation.foregroundEpoch !== foregroundEpoch) {
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
  // 隱藏時不查詢；啟用畫面仍由使用者主動恢復。
  if (document.visibilityState !== "visible" || !isServiceReady.value) {
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

// 靜態頁掛載後才讀取服務與登入狀態，保持 generate 不依賴後端。
onMounted(() => {
  // 初次狀態確認與手動恢復使用同一路徑，不呼叫模型或重試。
  void refreshSetupStatus();
  // 同時追蹤離開與返回，避免採用前一次停留的啟用狀態。
  window.addEventListener("blur", leaveForeground);
  window.addEventListener("focus", checkSetupOnForeground);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

// 離開頁面時取消目前操作，使遲到回應不能更新卸載後的畫面。
onBeforeUnmount(() => {
  // 停止接收焦點與可見性事件，避免卸載後開始新的查詢。
  window.removeEventListener("blur", leaveForeground);
  window.removeEventListener("focus", checkSetupOnForeground);
  document.removeEventListener("visibilitychange", handleVisibilityChange);

  // 先撤銷畫面更新權，再中止請求及其後續 readback。
  const operation = activeOperation.value;
  activeOperation.value = null;
  operation?.controller.abort();
});
</script>

<template>
  <main v-if="!isServiceReady" class="setup-shell" aria-labelledby="page-title">
    <header class="page-heading">
      <p class="eyebrow">AI 客服服務</p>
      <h1 id="page-title">客服工作台</h1>
      <p>確認本機服務啟用狀態，並依提示完成必要設定。</p>
    </header>

    <section class="setup-card" :aria-busy="isLoading">
      <p class="eyebrow">服務狀態</p>

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
      <p class="eyebrow">示範帳號登入</p>
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
      <aside class="workbench-rail">
        <div class="rail-brand">
          <p class="brand-mark">AI 客服</p>
          <p>本機服務工作區</p>
        </div>

        <div class="role-context">
          <p class="rail-label">目前角色</p>
          <p class="role-badge">{{ roleTitle }}</p>
        </div>

        <nav class="workbench-nav" aria-label="工作台導覽">
          <p class="rail-label">工作區</p>
          <span class="nav-current" aria-current="page">
            <span class="nav-indicator" aria-hidden="true"></span>
            工作台
          </span>
        </nav>

        <div class="rail-account">
          <p class="rail-label">目前登入</p>
          <p class="account-name">{{ identity.username }}</p>
          <button
            type="button"
            class="logout-action"
            :disabled="isAuthBusy"
            @click="submitLogout"
          >
            {{ isAuthActionBusy ? "登出中…" : "登出" }}
          </button>
        </div>
      </aside>

      <div class="workbench-main">
        <header class="workbench-heading">
          <p class="eyebrow">{{ roleWorkspace }}</p>
          <h1 ref="workbenchHeading" tabindex="-1">{{ roleTitle }}工作台</h1>
          <p>
            目前登入帳號：<strong>{{ identity.username }}</strong
            >。{{ roleDescription }}
          </p>
        </header>

        <p v-if="authMessage" class="workbench-feedback" role="status">
          {{ authMessage }}
        </p>

        <section class="empty-state" aria-labelledby="empty-state-title">
          <p class="eyebrow">角色已確認</p>
          <h2 id="empty-state-title">工作台已就緒</h2>
          <p>{{ emptyStateDescription }}</p>
        </section>

        <p class="workbench-note">
          登入狀態由本機服務管理；重新載入頁面時會重新確認目前角色。
        </p>
      </div>
    </section>
  </main>
</template>
