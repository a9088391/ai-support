export const SETUP_MODEL = "gemini-3.5-flash-lite" as const;
export const INVALID_PLAIN_TEXT_CHARACTER =
  /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\ud800-\udfff]/u;
export const BLANK_PLAIN_TEXT = /^[\p{Z}\t\n\r]*$/u;
const MAX_ANSWER_SOURCES = 40;
const MAX_TICKET_TEXT_CODE_POINTS = 4_000;
const RESOURCE_ID = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/;

// 搜尋、問答及工單共用來源DTO；契約層不依賴呈現元件。
export interface KnowledgeSearchResult {
  readonly document_id: string;
  readonly title: string;
  readonly version: string;
  readonly fragment_id: string;
  readonly excerpt: string;
}

export type Role = "user" | "support";

export const answerStatusMessages = {
  answered: "",
  insufficient_evidence:
    "本次搜尋沒有足夠的知識資料可回答，請查看已取得的搜尋依據，或由客服協助補充資料。",
  invalid_output: "本次回答未通過驗證。您可以查看已取得的搜尋結果。",
  rate_limited: "模型請求已達額度或速率上限，請稍後再手動重試。",
  timed_out: "本次問答已逾時。您可以查看已取得的搜尋結果。",
  unavailable: "模型暫時無法使用，請稍後再手動重試。",
  stopped: "回答未完成",
} as const;

type AnswerStatus = keyof typeof answerStatusMessages;

// 工作台與工單快照共用標題；待確認僅供前端呈現，不加入後端終態契約。
export const answerStatusTitles = {
  answered: "回答",
  insufficient_evidence: "知識資料不足",
  invalid_output: "模型回答未通過驗證",
  rate_limited: "模型額度或速率已達上限",
  timed_out: "問答已逾時",
  unavailable: "模型服務暫時無法使用",
  stopped: "回答未完成",
  unconfirmed: "回答結果待確認",
} as const satisfies Record<AnswerStatus | "unconfirmed", string>;

export interface AnswerIdentity {
  readonly conversation_id: string;
  readonly question_id: string;
  readonly attempt_id: string;
}

export interface AnswerEvidence {
  readonly answer: string | null;
  readonly citations: readonly KnowledgeSearchResult[];
  readonly search_results: readonly KnowledgeSearchResult[];
}

export interface AnswerOutput extends AnswerIdentity, AnswerEvidence {
  readonly status: Exclude<AnswerStatus, "stopped">;
}

export interface TicketSummary {
  readonly id: string;
  readonly description: string;
  readonly question: string;
  readonly created_at: string;
  readonly status: "open" | "closed";
  readonly resolution: string | null;
  readonly closed_at: string | null;
}

export interface TicketDetail extends TicketSummary, AnswerEvidence {
  readonly question_id: string;
  readonly attempt_id: string;
  readonly answer_status: AnswerStatus;
  readonly created_by: Role;
}

export interface TicketPage {
  readonly tickets: readonly TicketSummary[];
  readonly has_more: boolean;
}

// 搜尋及問答共用相同的片段欄位；各自的筆數上限由外層回應驗證。
export function isKnowledgeSearchResult(
  item: unknown,
): item is KnowledgeSearchResult {
  // 先拒絕陣列與 null，再檢查結果欄位。
  // prettier-ignore
  if (
    typeof item !== "object"
    || item === null
    || Array.isArray(item)
  ) {
    return false;
  }

  const result = item as Record<string, unknown>;

  // 每筆只接受 API 定義的五個純文字欄位。
  // prettier-ignore
  return (
    Object.keys(result).length === 5
    && typeof result.document_id === "string"
    && typeof result.title === "string"
    && typeof result.version === "string"
    && typeof result.fragment_id === "string"
    && typeof result.excerpt === "string"
  );
}

// 對話與工單都使用後端發出的不透明識別。
export function isResourceId(value: unknown): value is string {
  return typeof value === "string" && RESOURCE_ID.test(value);
}

// 僅從 JSON 物件讀取欄位，避免把 null 或陣列當成公開 DTO。
export function asRecord(value: unknown): Record<string, unknown> | null {
  // prettier-ignore
  return (
    typeof value === "object"
    && value !== null
    && !Array.isArray(value)
  ) ? value as Record<string, unknown> : null;
}

// 受理事件及問答終態共用同一組不透明識別。
export function isAnswerIdentity(value: unknown): value is AnswerIdentity {
  const dto = asRecord(value);
  // prettier-ignore
  return (
    dto !== null
    && isResourceId(dto.conversation_id)
    && isResourceId(dto.question_id)
    && isResourceId(dto.attempt_id)
  );
}

// 核對私有終態確實屬於這次已受理的問答。
export function sameAnswerAttempt(
  left: AnswerIdentity,
  right: AnswerIdentity,
): boolean {
  // prettier-ignore
  return (
    left.conversation_id === right.conversation_id
    && left.question_id === right.question_id
    && left.attempt_id === right.attempt_id
  );
}

// 問答終態和工單快照使用相同的回答、引用與搜尋證據契約。
function readEvidence(
  dto: Record<string, unknown>,
  status: string,
): AnswerEvidence | null {
  const { answer, citations, search_results } = dto;

  // 未知狀態或來源結構不應顯示為可用的可信證據。
  // prettier-ignore
  if (
    !Object.hasOwn(answerStatusMessages, status)
    || !Array.isArray(citations)
    || citations.length > MAX_ANSWER_SOURCES
    || !citations.every(isKnowledgeSearchResult)
    || !Array.isArray(search_results)
    || search_results.length > MAX_ANSWER_SOURCES
    || !search_results.every(isKnowledgeSearchResult)
  ) {
    return null;
  }

  // 成功回答須有完整文字及唯一引用，且引用來自同一次搜尋。
  if (status === "answered") {
    // 逐欄比較完整來源，允許同一片段在不同版本各自出現。
    const sourceKey = (source: KnowledgeSearchResult) =>
      JSON.stringify([
        source.document_id,
        source.title,
        source.version,
        source.fragment_id,
        source.excerpt,
      ]);
    const sources = new Set(search_results.map(sourceKey));
    const citedSources = new Set(citations.map(sourceKey));
    // prettier-ignore
    if (
      typeof answer !== "string"
      || BLANK_PLAIN_TEXT.test(answer)
      || citations.length === 0
      || citedSources.size !== citations.length
      || ![...citedSources].every(
        // 拒絕本次搜尋未包含的引用，避免顯示錯誤證據。
        (key) => sources.has(key),
      )
    ) {
      return null;
    }
  } else if (answer !== null || citations.length !== 0) {
    // 非成功快照可保留搜尋證據，但沒有可公開的回答或引用。
    return null;
  }

  return { answer, citations, search_results };
}

// 僅接受既有的問答終態；停止狀態由已提交工單證明。
export function readAnswerOutput(value: unknown): AnswerOutput | null {
  // 固定公開欄位、模型及識別，避免把受理事件當作模型終態。
  const dto = asRecord(value);
  // prettier-ignore
  if (
    !dto
    || Object.keys(dto).length !== 8
    || dto.model !== SETUP_MODEL
    || !isAnswerIdentity(dto)
    || typeof dto.status !== "string"
    || dto.status === "stopped"
  ) {
    return null;
  }

  // 只保留驗證過的欄位，不將其他服務資料傳入私人工作區。
  const evidence = readEvidence(dto, dto.status);
  if (!evidence) {
    return null;
  }
  return {
    conversation_id: dto.conversation_id,
    question_id: dto.question_id,
    attempt_id: dto.attempt_id,
    status: dto.status as AnswerOutput["status"],
    ...evidence,
  };
}

// 工單表單與結案讀回共用純文字規則，以碼位計數並保留原文。
export function validateTicketText(
  text: string,
  field: "問題描述" | "處理結果",
): string {
  // 拒絕超限文字，保留合法的代理字元對與草稿。
  if (
    text.length > MAX_TICKET_TEXT_CODE_POINTS * 2 ||
    Array.from(text).length > MAX_TICKET_TEXT_CODE_POINTS
  ) {
    return `${field}不可超過 4,000 個 Unicode 字元。`;
  }

  // 拒絕控制字元及未配對代理字元，保留允許的空白與換行。
  if (INVALID_PLAIN_TEXT_CHARACTER.test(text)) {
    return `${field}包含無法使用的字元，請修正後再送出。`;
  }

  // 純空白無法說明問題或結果；含有效內容時保留前後空白。
  return BLANK_PLAIN_TEXT.test(text) ? `請輸入${field}。` : "";
}

// 工單建立與結案時間都須為可顯示的 UTC 時間。
function isUtcTicketTime(value: unknown): value is string {
  // prettier-ignore
  return (
    typeof value === "string"
    && /(?:Z|\+00:00)$/.test(value)
    && Number.isFinite(Date.parse(value))
  );
}

// 摘要須包含可用的識別、描述、原始問題與完整的狀態欄位。
function isTicketSummary(value: unknown): value is TicketSummary {
  // 清單與快照都先驗證不隨狀態變更的欄位。
  const dto = asRecord(value);
  // prettier-ignore
  if (
    !dto
    || !isResourceId(dto.id)
    || typeof dto.description !== "string"
    || typeof dto.question !== "string"
    || !isUtcTicketTime(dto.created_at)
  ) {
    return false;
  }

  // 開啟時兩欄皆空；結案時間與合法的處理結果才能證明已結案。
  if (dto.status === "open") {
    return dto.resolution === null && dto.closed_at === null;
  }
  // prettier-ignore
  return (
    dto.status === "closed"
    && typeof dto.resolution === "string"
    && validateTicketText(dto.resolution, "處理結果") === ""
    && isUtcTicketTime(dto.closed_at)
  );
}

// 確認列表在要求的分頁大小內，避免接受錯誤資料形狀。
export function readTicketPage(
  value: unknown,
  limit: number,
): TicketPage | null {
  const dto = asRecord(value);
  // prettier-ignore
  if (
    !dto
    || !Array.isArray(dto.tickets)
    || dto.tickets.length > limit
    || !dto.tickets.every(isTicketSummary)
    || typeof dto.has_more !== "boolean"
  ) {
    return null;
  }
  return { tickets: dto.tickets, has_more: dto.has_more };
}

// 建單回應限本人；僅工單查閱和結案明確開放客服讀取跨帳號快照。
export function readTicketDetail(
  value: unknown,
  owner: Role,
  scope: "own" | "visible" = "own",
): TicketDetail | null {
  // 回應須包含基本工單資訊與來源識別，任何角色均拒絕未知建立人。
  const dto = asRecord(value);
  // prettier-ignore
  if (
    !dto
    || !isTicketSummary(dto)
    || !isResourceId(dto.question_id)
    || !isResourceId(dto.attempt_id)
    || typeof dto.answer_status !== "string"
    || (dto.created_by !== "user" && dto.created_by !== "support")
  ) {
    return null;
  }

  // 只有明確採用查閱範圍的客服可看到另一帳號的工單。
  if (
    !(scope === "visible" && owner === "support") &&
    dto.created_by !== owner
  ) {
    return null;
  }

  // 快照的內容由後端確定，前端與問答卡使用同一個證據檢查。
  const evidence = readEvidence(dto, dto.answer_status);
  if (!evidence) {
    return null;
  }
  return {
    id: dto.id,
    description: dto.description,
    created_at: dto.created_at,
    status: dto.status,
    resolution: dto.resolution,
    closed_at: dto.closed_at,
    question_id: dto.question_id,
    attempt_id: dto.attempt_id,
    question: dto.question,
    answer_status: dto.answer_status as AnswerStatus,
    created_by: dto.created_by,
    ...evidence,
  };
}

const taipeiTicketTime = new Intl.DateTimeFormat("zh-TW", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Asia/Taipei",
});

// 清單與詳情固定採臺灣時間，與瀏覽器所在的時區無關。
export function ticketTime(timestamp: string): string {
  return `${taipeiTicketTime.format(new Date(timestamp))}（臺灣時間）`;
}
