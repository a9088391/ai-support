<script setup lang="ts">
import { revealAnswer, type AnswerFrames } from "~/workbench/answer-reveal";

const props = defineProps<{ text: string }>();
const visibleText = ref(props.text);
let cancelReveal: (() => void) | null = null;
let motionPreference: MediaQueryList | null = null;

const frames: AnswerFrames = {
  // 與原生frame timestamp共用performance時鐘，不使用會受校時影響的日期。
  now() {
    return performance.now();
  },
  // 動畫只用原生frame，不增加背景計時器或傳輸工作。
  request(callback) {
    return requestAnimationFrame(callback);
  },
  // 元件離開或換答案時，釋放目前仍排程的frame。
  cancel(id) {
    cancelAnimationFrame(id);
  },
};

// 新答案先取消舊動畫；SSR與尚未掛載時直接保留完整文字。
function showAnswer(): void {
  cancelReveal?.();
  if (!motionPreference) {
    visibleText.value = props.text;
    return;
  }

  // 只有已驗證答案的可見副本會逐字改變，父層完整答案不受影響。
  cancelReveal = revealAnswer(
    props.text,
    motionPreference.matches,
    // 只更新本元件的可見文字，沒有事件或網路副作用。
    (text) => {
      visibleText.value = text;
    },
    frames,
  );
}

// 使用者啟用減少動態效果時立即全文；關閉偏好不重播已讀答案。
function handleMotionChange(event: MediaQueryListEvent): void {
  if (event.matches) {
    cancelReveal?.();
    cancelReveal = null;
    visibleText.value = props.text;
  }
}

// 換答案沿用同一取消入口，避免舊frame覆蓋新文字。
watch(() => props.text, showAnswer);

// 僅在瀏覽器掛載後讀取動態偏好及開始動畫，避免SSR存取window。
onMounted(() => {
  // 先讀取原生偏好，掛載當下即尊重reduced motion。
  motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  // 監聽偏好變更，使用者要求減少動畫時可立即停止。
  motionPreference.addEventListener("change", handleMotionChange);
  // 偏好就緒後才開始目前答案的可見動畫。
  showAnswer();
});

// 清對話、登出與卸載都由元件釋放frame及原生偏好監聽器。
onBeforeUnmount(() => {
  // 先封鎖晚到frame，再解除外部監聽，避免離開後仍更新可見值。
  cancelReveal?.();
  motionPreference?.removeEventListener("change", handleMotionChange);
  motionPreference = null;
});
</script>

<template>
  <!-- 全文隱形排版先保留高度；讀屏不隨每字更新重複播報。 -->
  <p class="answer-text answer-reveal" aria-live="off">
    <span class="answer-reveal-layout" aria-hidden="true">{{ text }}</span>
    <span class="answer-reveal-visible">{{ visibleText }}</span>
  </p>
</template>
