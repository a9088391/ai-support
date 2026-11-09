import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

import anyio
import psycopg
from google.genai._gaos.types.interactions import (
    FunctionCallStep,
    ModelOutputStep,
    StepParam,
    TextContent,
    ThoughtStep,
    UserInputStepParam,
)
from psycopg import Connection
from pydantic import Field, PositiveInt, TypeAdapter
from starlette.concurrency import run_in_threadpool

from backend import knowledge, sessions
from backend.answer_storage import (
    SIGN_IN,
    AnswerDenied,
    AnswerResponse,
    Attempt,
    AttemptDeadline,
    NewQuestion,
    RetryQuestion,
    bound_transaction,
    check_attempt_window,
    finish_attempt,
    lock_dispatch,
    lock_running_attempt,
    mark_timed_out,
    record_delivery,
    require_time,
    revoke_dispatch_session,
    search_attempt,
    start_attempt,
    stop_attempt,
)
from backend.credentials import CredentialError, read_key
from backend.database import Settings, reserve_request
from backend.provider import SEARCH_TOOL, AnswerExchange, Delivery, request_answer

ATTEMPT_SECONDS = 60.0
MODEL_LIMIT = 3
TOOL_LIMIT = 2


ANSWER_INPUT: TypeAdapter[NewQuestion | RetryQuestion] = TypeAdapter(
    NewQuestion | RetryQuestion
)


class ModelAnswer(knowledge.KnowledgeInput):
    evidence_sufficient: bool
    answer: knowledge.PlainText | None = Field(max_length=8000)
    citations: list[PositiveInt] = Field(max_length=TOOL_LIMIT * knowledge.RESULT_LIMIT)


# 預留交易不持有 Session 或啟用鎖；等待帳本期間到期則回滾整筆預留。
def reserve_business(
    settings: Settings, request_id: UUID, attempt: Attempt, deadline: float
) -> None:
    with settings.connect() as conn:
        bound_transaction(conn, deadline)
        # 共用預留先鎖用量及明細，與初始化先帳本再對話的順序相容。
        reserve_request(conn, request_id, username=attempt.access.identity.username)

        # 仍在同一交易；被接手或到期時整筆回滾，帳本不會留下暫時預留。
        bound_transaction(conn, deadline)
        window = lock_running_attempt(conn, attempt, deadline)
        check_attempt_window(window)
        require_time(deadline)


@dataclass
class SendBoundary:
    settings: Settings
    digest: bytes
    attempt: Attempt
    deadline: float
    request_id: UUID | None = None
    reserved: bool = False
    connection: Connection[tuple[object, ...]] | None = None
    sequence: int = 0
    delivery: Delivery = "unsent"

    # 在 HTTP/1.1 實際寫入前預留並驗證；鎖只涵蓋本機寫入，不等供應商回覆。
    async def trace(self, event: str, _info: dict[str, object]) -> None:
        if event == "http11.send_request_headers.started":
            # HTTP/1.1 單次請求只容許一個寫入邊界，防止意外自動再送。
            if self.request_id is not None:
                raise AnswerDenied(503, "Gemini 請求已停止。")
            self.request_id = uuid4()
            await run_in_threadpool(
                reserve_business,
                self.settings,
                self.request_id,
                self.attempt,
                self.deadline,
            )
            self.reserved = True

            # 預留與資料庫鎖均可能等待，必須在最後一把鎖後重新確認授權。
            (
                connection,
                self.sequence,
                wire_deadline,
                session_expiry,
            ) = await run_in_threadpool(
                lock_dispatch,
                self.settings,
                self.digest,
                self.attempt,
                self.deadline,
            )
            self.connection = connection
            await anyio.lowlevel.checkpoint()

            # 最後一次非同步等待後，換 Key 或登入到期只能退回尚未送出的預留。
            try:
                current_key = read_key(self.settings.key_file)
            except CredentialError:
                current_key = None
            if sessions.utc_now() >= session_expiry:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(
                        revoke_dispatch_session, connection, self.digest
                    )
                raise AnswerDenied(401, SIGN_IN)
            if current_key != self.attempt.access.key:
                raise AnswerDenied(503, "Gemini 設定已變更，請重新提出問題。")
            require_time(wire_deadline)

            # 寫入失敗或遭中斷時，無法確認已送出多少資料，維持不明。
            self.delivery = "unknown"
        elif event == "http11.send_request_body.complete":
            # 全部本文寫完即保留額度；回覆逾時、失敗或登出仍計次。
            self.delivery = "sent"
            self.close()
        elif event == "http11.receive_response_headers.complete":
            # 若傳輸在部分寫入後仍收到 HTTP 回覆，請求已確定送達。
            self.delivery = "sent"
        elif event in (
            "http11.send_request_headers.failed",
            "http11.send_request_body.failed",
        ):
            self.close()

    # 關閉沒有業務寫入的連線，釋放 PostgreSQL 授權及綁定共用鎖。
    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


# 單次 SDK 請求最後獨立結算額度，成功或中斷都不把鎖留到下個步驟。
async def exchange_once(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    history: list[StepParam],
    deadline: float,
) -> AnswerExchange:
    boundary = SendBoundary(settings, digest, attempt, deadline)
    rejected = None
    try:
        exchange = await request_answer(
            attempt.access.key.value, history, boundary.trace
        )
        if exchange.failure == "credential_rejected" and boundary.delivery != "unsent":
            rejected = (attempt.access.key.version, boundary.sequence)
        return exchange
    finally:
        # 中斷只釋放已知尚未寫入的預留；已寫入或送達不明仍有帳務紀錄。
        boundary.close()
        if boundary.reserved and boundary.request_id is not None:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(
                    record_delivery,
                    settings,
                    boundary.request_id,
                    boundary.delivery,
                    rejected,
                )


# 使用嚴格 UTF-8 解析原始供應商步驟，避免重複欄位或 SDK 重整簽章資料。
def received_steps(exchange: AnswerExchange) -> list[dict[str, object]]:
    body = knowledge.parse_json(exchange.body)
    match body:
        case {"steps": list(steps)} if steps and all(
            isinstance(step, dict) for step in steps
        ):
            return cast(list[dict[str, object]], steps)
    raise ValueError("供應商步驟格式不正確。")


# 僅辨識本題明示的單一權限問句；完整比對避免推斷前文或其他操作。
def permission_question(question: str) -> tuple[str, str, str] | None:
    # 將限定的明示用語映射到匯入事實欄位，不採信模型解析或工具查詢。
    roles = {
        "檢視者": "viewer",
        "工作區檢視者": "viewer",
        "編輯者": "editor",
        "擁有者": "owner",
        "工作區擁有者": "owner",
    }
    actions = {
        "閱讀": "read",
        "檢視": "read",
        "查看": "read",
        "匯出": "export",
        "修改": "modify",
        "編輯": "modify",
    }
    resources = {"共享報表": "shared_reports", "工作區設定": "workspace_settings"}
    # 整句必須只有一組角色、操作與資源；其他句型回到既有回答流程。
    match = re.fullmatch(
        r"(?:我是\s*)?(工作區檢視者|檢視者|編輯者|工作區擁有者|擁有者)\s*[，,]?\s*"
        r"(?:是否可以|是否能|可不可以|能不能|能否|可以|能|可)\s*"
        r"(閱讀|檢視|查看|匯出|修改|編輯)\s*(共享報表|工作區設定)\s*(?:嗎|呢)?[？?]?",
        question.strip(),
    )
    if match is None:
        return None
    return roles[match[1]], actions[match[2]], resources[match[3]]


# 複用單題角色解析，只辨識明示閱讀報表及編輯設定申請的兩句組合。
def compound_permission_question(question: str) -> str | None:
    match = re.fullmatch(
        r"(.+?[？?])\s*(?:如果|若)?(?:需要|想要|要)(?:修改|編輯)"
        r"(?:工作區)?設定\s*[，,]?\s*(?:應該|應|要)?(?:找誰|向誰申請)[？?]?",
        question.strip(),
    )
    if match is None:
        return None
    requested = permission_question(match[1])
    if requested is None or requested[1:] != ("read", "shared_reports"):
        return None
    return requested[0]


# 僅以實際引用的同版事實組裝三項；缺漏或衝突時不以模型文字補足。
def compound_permission_answer(
    role: str, cited: list[knowledge.SearchEvidence]
) -> str | None:
    # 同文件不同版本不得互補；整段限定文法避免以相鄰或被否定的片語當證據。
    label = {"viewer": "檢視者", "editor": "編輯者", "owner": "工作區擁有者"}[role]
    versions: dict[str, str] = {}
    visible_read: set[str] = set()
    visible_modify: set[str] = set()
    visible_recipients: set[str] = set()
    for item in cited:
        previous = versions.setdefault(item.source.document_id, item.source.version)
        if previous != item.source.version:
            return None
        # 只忽略匯入允許的首尾空白，公開引用與搜尋快照仍保留原文。
        match = re.fullmatch(
            rf"{label}(?:(可|無法)閱讀共享報表(?:[，。](?:{label})?(可|無法)修改工作區設定)?"
            r"|(可|無法)修改工作區設定)。?"
            r"(?:需要編輯時，請向([^。，]+)申請。)?",
            item.source.excerpt.strip(),
        )
        if match is None:
            return None
        effects = {"可": "allow", "無法": "deny", None: None}
        reading = effects[match[1]]
        modifying = effects[match[2] or match[3]]
        if match[4] is not None and modifying != "deny":
            return None
        if reading is not None:
            visible_read.add(reading)
        if modifying is not None:
            visible_modify.add(modifying)
        if match[4] is not None:
            visible_recipients.add(match[4])
        for fact in item.permissions:
            if fact.role != role:
                continue
            if (fact.action, fact.resource) == ("read", "shared_reports"):
                if fact.effect != reading:
                    return None
            elif (fact.action, fact.resource) == ("modify", "workspace_settings"):
                if fact.effect != modifying:
                    return None
                if (
                    fact.application_recipient is not None
                    and fact.application_recipient.label != match[4]
                ):
                    return None

    # 分別核對閱讀、修改與修改權限的申請對象，包括跨引用片段的矛盾。
    facts = [fact for item in cited for fact in item.permissions if fact.role == role]
    read = {
        fact.effect
        for fact in facts
        if (fact.action, fact.resource) == ("read", "shared_reports")
    }
    modify = [
        fact
        for fact in facts
        if (fact.action, fact.resource) == ("modify", "workspace_settings")
    ]
    if len(read) != 1 or not modify:
        return None
    if any(fact.effect != "deny" for fact in modify):
        return None
    recipients = {
        (fact.application_recipient.role, fact.application_recipient.label)
        for fact in modify
        if fact.application_recipient is not None
    }
    if (
        len(recipients) != 1
        or visible_read != read
        or visible_modify != {"deny"}
        or visible_recipients != {label for _, label in recipients}
    ):
        return None

    # 角色與操作來自有界問句；允許／禁止和申請標籤則必須來自已驗證事實。
    reading = "可" if read == {"allow"} else "無法"
    recipient = next(iter(recipients))[1]
    return f"{label}{reading}閱讀共享報表，無法修改工作區設定。需要編輯時，請向{recipient}申請。"


# 最終輸出只接受一個文字區塊及可驗證的 JSON；引用從本次工具結果還原。
def resolve_answer(
    output: ModelOutputStep,
    evidence: list[knowledge.SearchEvidence],
    response: AnswerResponse,
    question: str,
) -> None:
    # 不把部分文字、供應商錯誤或非文字內容視為可交付的回答。
    if (
        output.error is not None
        or not output.content
        or len(output.content) != 1
        or not isinstance(output.content[0], TextContent)
        or not isinstance(output.content[0].text, str)
    ):
        return
    result = ModelAnswer.model_validate(
        knowledge.parse_json(output.content[0].text.encode("utf-8"))
    )

    # 依據不足優先於相鄰資訊；清空模型答案和引用，但保留本輪搜尋快照。
    citations = result.citations
    if not result.evidence_sufficient:
        response.status = "insufficient_evidence"
        response.answer = None
        response.citations = []
    elif (
        result.answer is not None
        and citations
        and len(citations) == len(set(citations))
        and all(identifier <= len(evidence) for identifier in citations)
    ):
        # 先確認輸出及本輪引用有效，再獨立比對實際引用快照的完整權限事實。
        cited = [evidence[identifier - 1] for identifier in citations]
        compound_role = compound_permission_question(question)
        if compound_role is not None:
            response.answer = compound_permission_answer(compound_role, cited)
            response.status = (
                "answered" if response.answer is not None else "insufficient_evidence"
            )
            response.citations = (
                [item.source for item in cited] if response.answer is not None else []
            )
            return
        requested = permission_question(question)
        if requested is not None and not any(
            (fact.role, fact.action, fact.resource) == requested
            for item in cited
            for fact in item.permissions
        ):
            response.status = "insufficient_evidence"
            response.answer = None
            response.citations = []
            return
        response.answer = result.answer
        response.citations = [item.source for item in cited]
        response.status = "answered"


# 只執行 allowlist 工具，回傳本次呼叫的真實搜尋快照及逐筆證據識別。
async def run_search(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    query: str,
    evidence: list[knowledge.SearchEvidence],
    deadline: float,
) -> str:
    results = await run_in_threadpool(
        search_attempt, settings, digest, attempt, query, deadline
    )

    # 工具交易已保存可信證據；執行緒回傳值只用來分配本次交換的引用識別。
    for item in results:
        if not any(existing.source == item.source for existing in evidence):
            evidence.append(item)

    # 逾時時只保留已取得的證據，不再建立送給模型的下一步。
    await anyio.lowlevel.checkpoint()
    require_time(deadline)

    # 相同公開來源沿用首次權限快照及識別；版本或文字不同才建立新的證據。
    sources = [item.source for item in evidence]
    output = [
        {"evidence_id": sources.index(item.source) + 1, **item.source.model_dump()}
        for item in results
    ]
    return json.dumps(output, ensure_ascii=False)


# 手動處理有限步驟；先驗證整批工具要求才執行，不採信模型聲稱做過的搜尋。
async def run_attempt(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    response: AnswerResponse,
    deadline: float,
) -> list[StepParam] | None:
    # 前文取自資料庫；本題工具證據與已執行工具數量都從空白開始。
    history = list(attempt.history)
    user_input: UserInputStepParam = {
        "type": "user_input",
        "content": [{"type": "text", "text": attempt.question}],
    }
    if attempt.delivered_answers:
        # 使用者只能提供第一區塊；後端另附差異脈絡，且不加入本輪工具證據。
        user_input["content"].append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "previous_delivered_answers": attempt.delivered_answers,
                        "context_notice": "先前實際交付的答案僅供理解追問，不是本題的引用證據。",
                    },
                    ensure_ascii=False,
                ),
            }
        )
    history.append(user_input)
    calls_seen: set[str] = set()
    evidence: list[knowledge.SearchEvidence] = []

    # 每輪只可能有一次網路請求，配額或供應商錯誤立即停止。
    for request_index in range(MODEL_LIMIT):
        exchange = await exchange_once(settings, digest, attempt, history, deadline)
        result = exchange.result
        if result is None:
            response.status = (
                "unavailable"
                if exchange.failure == "credential_rejected"
                else exchange.failure
            )
            return None

        # 拒絕未完成或無法原樣回傳的步驟；不接受模型自己產生的工具結果。
        raw = received_steps(exchange)
        steps = result.steps or []
        if len(steps) != len(raw):
            return None
        for step, value in zip(steps, raw, strict=True):
            if not isinstance(step, (ThoughtStep, FunctionCallStep, ModelOutputStep)):
                return None
            if step.model_dump(mode="json", by_alias=True, exclude_unset=True) != value:
                return None

        # 忽略思考內容的語義，但後續傳輸必須完整保留原順序及簽章。
        actions = [step for step in steps if not isinstance(step, ThoughtStep)]
        if (
            result.status == "completed"
            and len(actions) == 1
            and isinstance(actions[0], ModelOutputStep)
        ):
            resolve_answer(actions[0], evidence, response, attempt.question)
            # 保存單題的完整交換；前文不重複寫入下一題。
            history.extend(cast(list[StepParam], raw))
            return history[len(attempt.history) :]
        if (
            result.status != "requires_action"
            or not actions
            or request_index + 1 >= MODEL_LIMIT
        ):
            return None
        if len(calls_seen) + len(actions) > TOOL_LIMIT:
            return None

        # 名稱、參數及呼叫 ID 全部通過才進入資料庫；重複 ID 不能冒用結果。
        batch: list[tuple[str, str]] = []
        for action in actions:
            if (
                not isinstance(action, FunctionCallStep)
                or action.name != SEARCH_TOOL
                or not isinstance(action.id, str)
                or not action.id.strip()
                or len(action.id) > 256
                or action.id in calls_seen
            ):
                return None
            query = knowledge.SearchInput.model_validate(action.arguments).query.strip()
            calls_seen.add(action.id)
            batch.append((action.id, query))

        # 完整帶回供應商的步驟，逐筆附上本地唯讀查詢結果。
        history.extend(cast(list[StepParam], raw))
        for call_id, query in batch:
            output = await run_search(
                settings, digest, attempt, query, evidence, deadline
            )
            history.append(
                {
                    "type": "function_result",
                    "name": SEARCH_TOOL,
                    "call_id": call_id,
                    "result": [{"type": "text", "text": output}],
                }
            )
    return None


# 總期限涵蓋同一嘗試的所有外呼與工具；終止後只結算，不允許背景改寫答案。
async def ask(
    settings: Settings,
    digest: bytes,
    form: NewQuestion | RetryQuestion,
    disconnected: Event,
    admitted: Callable[[Attempt], Awaitable[None]] | None = None,
) -> AnswerResponse:
    # 單調時間控制本程序；UTC 期限讓其他程序只接手確定已逾期的嘗試。
    deadline_at = datetime.now(UTC) + timedelta(seconds=ATTEMPT_SECONDS)
    deadline = monotonic() + ATTEMPT_SECONDS
    attempt = await run_in_threadpool(
        start_attempt, settings, digest, form, deadline_at
    )
    response = AnswerResponse(
        conversation_id=attempt.conversation_id,
        question_id=attempt.question_id,
        attempt_id=attempt.id,
        status="invalid_output",
        citations=[],
        search_results=[],
    )
    finished = False
    model_steps = None
    try:
        # 串流只在受理提交後發出識別；傳送中斷仍經同一個收尾路徑停止嘗試。
        if admitted is not None:
            with anyio.move_on_after(max(0, deadline - monotonic())) as emission:
                await admitted(attempt)
            if emission.cancel_called:
                raise AnswerDenied(503, "目前無法傳送問答受理資訊。")

        # 只取消本次操作；送達結算及終態寫入在期限取消後仍可完成。
        with anyio.move_on_after(max(0, deadline - monotonic())) as timeout:
            try:
                model_steps = await run_attempt(
                    settings, digest, attempt, response, deadline
                )
            except AttemptDeadline:
                mark_timed_out(response)
            except (ValueError, RecursionError):
                response.status = "invalid_output"
            except psycopg.Error:
                # 執行失敗可保留先前證據；終態交易仍須獨立驗證登入與啟用。
                response.status = "unavailable"
        if timeout.cancel_called:
            mark_timed_out(response)

        # 最後一次持鎖確認擁有者、啟用及期限，拒絕任何過期的私人交付。
        await run_in_threadpool(
            finish_attempt,
            settings,
            digest,
            attempt,
            response,
            model_steps,
            deadline,
            disconnected,
        )
        finished = True
        await anyio.lowlevel.checkpoint()
        return response
    finally:
        # 登出、HTTP 中斷與關閉服務都只能保留已送出帳務及非成功的終止狀態。
        if not finished:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(stop_attempt, settings, attempt)
