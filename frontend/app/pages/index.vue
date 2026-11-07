<script setup lang="ts">
const SETUP_MODEL = "gemini-3.5-flash-lite" as const;

const setupStatuses = [
  "unconfigured",
  "unverified",
  "ready",
  "invalid",
] as const;

type SetupStatus = (typeof setupStatuses)[number];

interface SetupDto {
  readonly status: SetupStatus;
  readonly message: string;
  readonly model: typeof SETUP_MODEL;
}

const statusTitles: Record<SetupStatus, string> = {
  unconfigured: "尚未設定 API Key",
  unverified: "設定尚未通過驗證",
  ready: "服務已啟用",
  invalid: "API Key 無效",
};

const setupStatus = ref<SetupDto | null>(null);
const isLoading = ref(false);
const hasRequestError = ref(false);
let activeRequest: AbortController | null = null;

// 僅接受完整且符合本票固定契約的狀態回應，避免未知內容被誤認為已啟用。
function isSetupDto(value: unknown): value is SetupDto {
  // 先拒絕非物件與陣列，再安全檢查物件欄位。
  // prettier-ignore
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
  ) {
    return false;
  }

  const dto = value as Record<string, unknown>;

  // 回應只接受核准的三個欄位及固定模型，避免外部形狀被默默忽略。
  // prettier-ignore
  if (
    Object.keys(dto).length !== 3
    || !('status' in dto)
    || !('message' in dto)
    || !('model' in dto)
  ) {
    return false;
  }

  // 狀態必須是目前明確支援的四種之一，訊息則只會以純文字顯示。
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

// 重新讀取唯讀狀態；新請求取得畫面更新權，避免舊回應覆蓋較新的結果。
async function refreshSetupStatus(): Promise<void> {
  // 先取消舊請求，再登記新請求作為目前唯一可更新畫面的請求。
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;

  // 重新確認期間不展示先前的 ready 結果，避免把過期狀態當成當前狀態。
  hasRequestError.value = false;
  isLoading.value = true;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;

  try {
    // 本機服務若十秒內沒有回覆就中止，讓操作人員可恢復手動確認。
    timeoutId = setTimeout(() => {
      controller.abort();
    }, 10_000);

    // 僅呼叫狀態 GET、不自動重試且停用瀏覽器快取；此頁不呼叫模型。
    const response = await $fetch<unknown>("/api/setup", {
      method: "GET",
      cache: "no-store",
      retry: 0,
      signal: controller.signal,
    });

    // 舊請求即使遲到也不可更動新請求擁有的狀態。
    if (activeRequest !== controller) {
      return;
    }

    // 合約外的回應一律清除狀態並要求操作者手動重試。
    if (!isSetupDto(response)) {
      setupStatus.value = null;
      hasRequestError.value = true;
      return;
    }

    // 只有目前請求取得完整合法 DTO 時才展示後端狀態。
    setupStatus.value = response;
  } catch {
    // 取消或過期請求不得清除由新請求擁有的狀態。
    if (activeRequest !== controller) {
      return;
    }

    // 503、網路錯誤、逾時與解析失敗都採關閉狀態，不展示舊結果或技術細節。
    setupStatus.value = null;
    hasRequestError.value = true;
  } finally {
    // 無論請求是否過期都清除自己的逾時計時器，避免卸載後仍保留計時工作。
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
    }

    // 只有仍持有請求權的工作可以結束畫面忙碌狀態。
    if (activeRequest === controller) {
      isLoading.value = false;
      activeRequest = null;
    }
  }
}

// 靜態頁只在瀏覽器載入後查詢一次，避免產生階段依賴執行中的 API。
onMounted(() => {
  void refreshSetupStatus();
});

// 離開頁面時取消尚未完成的請求，避免它之後更新已卸載頁面的狀態。
onBeforeUnmount(() => {
  // 清除請求擁有權後再中止，讓延遲的 catch/finally 無法改寫畫面狀態。
  activeRequest?.abort();
  activeRequest = null;
});
</script>

<template>
  <main class="setup-shell" aria-labelledby="page-title">
    <header class="page-heading">
      <h1 id="page-title">客服工作台</h1>
      <p>確認本機服務啟用狀態，並依提示完成必要設定。</p>
    </header>

    <section class="setup-card" :aria-busy="isLoading">
      <p class="eyebrow">服務狀態</p>

      <div v-if="isLoading" class="status-content" role="status">
        <h2>正在確認服務狀態</h2>
        <p>正在讀取本機服務狀態。</p>
      </div>

      <div v-else-if="hasRequestError" class="status-content" role="alert">
        <h2>目前無法確認狀態</h2>
        <p>目前無法取得有效的服務狀態，請確認本機服務後手動重新確認。</p>
      </div>

      <div v-else-if="setupStatus" class="status-content" aria-live="polite">
        <h2>{{ statusTitles[setupStatus.status] }}</h2>
        <p>{{ setupStatus.message }}</p>
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

      <div class="refresh-row">
        <button type="button" :disabled="isLoading" @click="refreshSetupStatus">
          {{ isLoading ? "確認中…" : "重新確認狀態" }}
        </button>
      </div>
    </section>
  </main>
</template>
