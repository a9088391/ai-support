import json
from dataclasses import dataclass
from datetime import date
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
)
from psycopg import Connection
from pydantic import Field, PositiveInt
from starlette.concurrency import run_in_threadpool

from backend import knowledge
from backend.answer_storage import (
    AnswerDenied,
    AnswerResponse,
    Attempt,
    AttemptDeadline,
    bound_transaction,
    finish_attempt,
    lock_dispatch,
    mark_timed_out,
    record_delivery,
    require_time,
    search_attempt,
    start_attempt,
    stop_attempt,
)
from backend.credentials import CredentialError, read_key
from backend.database import Settings, reserve_request, service_day_now
from backend.provider import SEARCH_TOOL, AnswerExchange, Delivery, request_answer

ATTEMPT_SECONDS = 60.0
MODEL_LIMIT = 3
TOOL_LIMIT = 2


class AnswerInput(knowledge.KnowledgeInput):
    question: knowledge.PlainText = Field(max_length=2000)


class ModelAnswer(knowledge.KnowledgeInput):
    answer: knowledge.PlainText | None = Field(max_length=8000)
    citations: list[PositiveInt] = Field(max_length=TOOL_LIMIT * knowledge.RESULT_LIMIT)


class QuotaExhausted(Exception):
    pass


# 預留交易不持有 Session 或啟用列鎖；提交後才允許 HTTP 寫入。
def reserve_business(
    settings: Settings, request_id: UUID, username: str, deadline: float
) -> date | None:
    with settings.connect() as conn:
        bound_transaction(conn, deadline)
        return reserve_request(conn, request_id, username=username)


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
            reserved_day = await run_in_threadpool(
                reserve_business,
                self.settings,
                self.request_id,
                self.attempt.access.identity.username,
                self.deadline,
            )
            if reserved_day is None:
                raise QuotaExhausted
            self.reserved = True

            # 預留與資料庫鎖均可能等待，必須在最後一把鎖後重新確認授權。
            self.connection, self.sequence, wire_deadline = await run_in_threadpool(
                lock_dispatch,
                self.settings,
                self.digest,
                self.attempt.access,
                self.deadline,
            )
            await anyio.lowlevel.checkpoint()

            # 最後一次非同步等待後，跨日或換 Key 的已預留請求仍可安全取消。
            try:
                current_key = read_key(self.settings.key_file)
            except CredentialError:
                current_key = None
            if current_key != self.attempt.access.key:
                raise AnswerDenied(503, "Gemini 設定已變更，請重新提出問題。")
            if service_day_now() != reserved_day:
                raise QuotaExhausted
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


# 最終輸出只接受一個文字區塊及可驗證的 JSON；引用從本次工具結果還原。
def resolve_answer(
    output: ModelOutputStep,
    evidence: list[knowledge.SearchResult],
    response: AnswerResponse,
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

    # 空回答僅能搭配空引用；成功回答須有唯一且來自本次搜尋的證據。
    citations = result.citations
    if result.answer is None and not citations:
        response.status = "insufficient_evidence"
    elif (
        result.answer is not None
        and citations
        and len(citations) == len(set(citations))
        and all(identifier <= len(evidence) for identifier in citations)
    ):
        response.answer = result.answer
        response.citations = [evidence[identifier - 1] for identifier in citations]
        response.status = "answered"


# 只執行 allowlist 工具，回傳本次呼叫的真實搜尋快照及逐筆證據識別。
async def run_search(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    query: str,
    evidence: list[knowledge.SearchResult],
    deadline: float,
) -> str:
    results = await run_in_threadpool(
        search_attempt, settings, digest, attempt, query, deadline
    )

    # 工具交易已確認證據在期限內且仍可讀；先保留快照再交回可取消的事件迴圈。
    for source in results:
        if source not in evidence:
            evidence.append(source)

    # 逾時時只保留已取得的證據，不再建立送給模型的下一步。
    await anyio.lowlevel.checkpoint()
    require_time(deadline)

    # 相同來源快照共用識別，文件更新後即使片段 ID 相同仍是不同證據。
    output = [
        {"evidence_id": evidence.index(source) + 1, **source.model_dump()}
        for source in results
    ]
    return json.dumps(output, ensure_ascii=False)


# 手動處理有限步驟；先驗證整批工具要求才執行，不採信模型聲稱做過的搜尋。
async def run_attempt(
    settings: Settings,
    digest: bytes,
    attempt: Attempt,
    question: str,
    response: AnswerResponse,
    deadline: float,
) -> None:
    history: list[StepParam] = [
        {"type": "user_input", "content": [{"type": "text", "text": question}]}
    ]
    calls_seen: set[str] = set()

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
            return

        # 拒絕未完成或無法原樣回傳的步驟；不接受模型自己產生的工具結果。
        raw = received_steps(exchange)
        steps = result.steps or []
        if len(steps) != len(raw):
            return
        for step, value in zip(steps, raw, strict=True):
            if not isinstance(step, (ThoughtStep, FunctionCallStep, ModelOutputStep)):
                return
            if step.model_dump(mode="json", by_alias=True, exclude_unset=True) != value:
                return

        # 忽略思考內容的語義，但後續傳輸必須完整保留原順序及簽章。
        actions = [step for step in steps if not isinstance(step, ThoughtStep)]
        if (
            result.status == "completed"
            and len(actions) == 1
            and isinstance(actions[0], ModelOutputStep)
        ):
            resolve_answer(actions[0], response.search_results, response)
            return
        if (
            result.status != "requires_action"
            or not actions
            or request_index + 1 >= MODEL_LIMIT
        ):
            return
        if len(calls_seen) + len(actions) > TOOL_LIMIT:
            return

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
                return
            query = knowledge.SearchInput.model_validate(action.arguments).query.strip()
            calls_seen.add(action.id)
            batch.append((action.id, query))

        # 完整帶回供應商的步驟，逐筆附上本地唯讀查詢結果。
        history.extend(cast(list[StepParam], raw))
        for call_id, query in batch:
            output = await run_search(
                settings, digest, attempt, query, response.search_results, deadline
            )
            history.append(
                {
                    "type": "function_result",
                    "name": SEARCH_TOOL,
                    "call_id": call_id,
                    "result": [{"type": "text", "text": output}],
                }
            )


# 總期限涵蓋同一嘗試的所有外呼與工具；終止後只結算，不允許背景改寫答案。
async def ask(
    settings: Settings, digest: bytes, question: str, disconnected: Event
) -> AnswerResponse:
    deadline = monotonic() + ATTEMPT_SECONDS
    attempt = await run_in_threadpool(start_attempt, settings, digest, question)
    response = AnswerResponse(
        conversation_id=attempt.conversation_id,
        question_id=attempt.question_id,
        attempt_id=attempt.id,
        status="invalid_output",
        citations=[],
        search_results=[],
    )
    finished = False
    try:
        # 只取消本次操作；送達結算及終態寫入在期限取消後仍可完成。
        with anyio.move_on_after(max(0, deadline - monotonic())) as timeout:
            try:
                await run_attempt(
                    settings, digest, attempt, question, response, deadline
                )
            except AttemptDeadline:
                mark_timed_out(response)
            except QuotaExhausted:
                response.status = "rate_limited"
            except (ValueError, RecursionError):
                response.status = "invalid_output"
            except psycopg.Error:
                # 執行失敗可保留先前證據；終態交易仍須獨立驗證登入與啟用。
                response.status = "unavailable"
        if timeout.cancel_called:
            mark_timed_out(response)

        # 最後一次持鎖確認擁有者、啟用及期限，拒絕任何過期的私人交付。
        await run_in_threadpool(
            finish_attempt, settings, digest, attempt, response, deadline, disconnected
        )
        finished = True
        await anyio.lowlevel.checkpoint()
        return response
    finally:
        # 登出、HTTP 中斷與關閉服務都只能保留已送出帳務及非成功的終止狀態。
        if not finished:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(stop_attempt, settings, attempt)
