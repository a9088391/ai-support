export interface AnswerFrames {
  now(): number;
  request(callback: (timestamp: number) => void): number;
  cancel(id: number): void;
}

// 只逐字呈現已驗證的完整答案；frame介面供原生瀏覽器與可控時鐘共用。
export function revealAnswer(
  text: string,
  reducedMotion: boolean,
  render: (text: string) => void,
  frames: AnswerFrames,
): () => void {
  // 每份答案各自持有有效旗標與frame，不影響其他已顯示的答案。
  let active = true;
  let frame: number | null = null;

  // 取消排程並封鎖已被取出的舊callback，避免換題或卸載後晚寫。
  function cancel(): void {
    active = false;
    if (frame !== null) {
      frames.cancel(frame);
      frame = null;
    }
  }

  // 無動畫偏好與空答案直接完成，不建立不必要的排程或字素陣列。
  if (reducedMotion || text === "") {
    active = false;
    render(text);
    return cancel;
  }

  // 以字素切分，保留中文、代理對、ZWJ表情、旗幟與組合字的完整性。
  const characters = Array.from(
    new Intl.Segmenter("zh-Hant", { granularity: "grapheme" }).segment(text),
    // 每次呈現一個完整字素，不依UTF-16碼元截斷。
    (part) => part.segment,
  );
  // 每字素約24ms，長文仍限制於1.2秒，使用同一個單調時鐘起點。
  const duration = Math.min(1200, characters.length * 24);
  const started = frames.now();
  let shown = 0;
  // 首次先清空可見副本，完整排版高度由元件保留。
  render("");

  // 依實際時間前進；背景分頁回來時直接追上，長文最多約一點二秒。
  function advance(timestamp: number): void {
    // 舊frame即使無法及時取消，也不能更新目前元件。
    if (!active) {
      return;
    }

    // 終態使用原始完整文字，動畫前綴不會改寫父層保存的答案。
    const count = Math.min(
      characters.length,
      Math.floor(
        (Math.max(0, timestamp - started) / duration) * characters.length,
      ),
    );
    if (count === characters.length) {
      active = false;
    }
    if (count !== shown) {
      shown = count;
      render(
        count === characters.length
          ? text
          : characters.slice(0, count).join(""),
      );
    }

    // 呈現過程若被取消，不再建立下一個frame；完成後也不留排程。
    frame = active ? frames.request(advance) : null;
  }

  // 首個frame由瀏覽器安排，取消函式由元件生命週期持有。
  frame = frames.request(advance);
  return cancel;
}
