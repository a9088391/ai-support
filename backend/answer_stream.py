import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from threading import Event

import anyio
import psycopg
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from backend import answers
from backend.answer_storage import (
    AnswerDenied,
    AnswerResponse,
    Attempt,
    NewQuestion,
    RetryQuestion,
)

NDJSON = "application/x-ndjson"
ANSWER_UNAVAILABLE = "目前無法完成問答。"


# 斷線取消操作本身，讓模型傳輸、配額結算及嘗試停止沿同一條路徑收尾。
async def watch_disconnect(
    request: Request, stop: anyio.CancelScope, disconnected: Event
) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass
    disconnected.set()
    stop.cancel()


# JSON 和串流回應都只讓一個監看者讀取斷線，且不留下背景問答工作。
async def run_answer(
    request: Request,
    digest: bytes,
    form: NewQuestion | RetryQuestion,
    admitted: Callable[[Attempt], Awaitable[None]] | None = None,
) -> AnswerResponse:
    with anyio.CancelScope() as stop:
        disconnected = Event()
        observer = asyncio.create_task(watch_disconnect(request, stop, disconnected))
        try:
            return await answers.ask(
                request.app.state.settings, digest, form, disconnected, admitted
            )
        finally:
            # 結束前回收唯一接收者；斷線造成的取消不能跳過此步驟。
            observer.cancel()
            with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
                await observer
    raise AnswerDenied(499, "已停止等待。")


class AnswerStream(Response):
    # 串流尚未受理時不開始 HTTP 回覆，讓前置拒絕保留原本的 HTTP 狀態。
    def __init__(self, digest: bytes, form: NewQuestion | RetryQuestion) -> None:
        self.digest = digest
        self.form = form
        self.status_code = 200
        self.media_type = NDJSON
        self.background = None
        self.init_headers()

    # 直接依 ASGI 順序送出受理及終態，沿用單一斷線監看，不再開另一個讀取者。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        # NDJSON 每列都是完整的公開事件；最後一列結束 HTTP 本文。
        async def send_frame(
            event: dict[str, object], *, terminal: bool = False
        ) -> None:
            body = (
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            await send(
                {"type": "http.response.body", "body": body, "more_body": not terminal}
            )

        # 只送已提交的識別，且在模型開始前交給 HTTP 傳輸。
        async def admitted(attempt: Attempt) -> None:
            nonlocal started
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            started = True
            await send_frame(
                {
                    "type": "accepted",
                    "conversation_id": str(attempt.conversation_id),
                    "question_id": str(attempt.question_id),
                    "attempt_id": str(attempt.id),
                }
            )

        # 原操作持有取消及收尾責任；寫回時若連線已消失就不再嘗試送出。
        with suppress(OSError):
            try:
                result = await run_answer(
                    Request(scope, receive), self.digest, self.form, admitted
                )
            except (AnswerDenied, psycopg.Error) as error:
                if isinstance(error, AnswerDenied):
                    status, detail = error.code, error.detail
                else:
                    status, detail = 503, ANSWER_UNAVAILABLE

                # 真正斷線不交付私有終態；尚未開始時回空白 499 供中介層收尾。
                if status == 499:
                    if not started:
                        await Response(status_code=499)(scope, receive, send)
                elif started:
                    await send_frame(
                        {"type": "error", "status": status, "detail": detail},
                        terminal=True,
                    )
                else:
                    await JSONResponse({"detail": detail}, status_code=status)(
                        scope, receive, send
                    )
            else:
                await send_frame(
                    {"type": "result", "result": result.model_dump(mode="json")},
                    terminal=True,
                )
