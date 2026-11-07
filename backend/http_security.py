import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOCAL_HOSTS = ("localhost", "127.0.0.1")
HOST_FORMAT = re.compile(r"([a-zA-Z0-9.]+)(?::([1-9][0-9]{0,4}))?")
BODY_LIMIT = 64 * 1024


class ApiRequestMiddleware:
    # 在路由及資料庫前攔截 API，健康檢查與靜態資源沿用既有入口。
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # 先檢查來源和有界本文，再重放完整本文給 FastAPI 解析。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 非 API 請求直接交給原本的 Host 檢查及路由。
        path = scope["path"] if scope["type"] == "http" else ""
        if path != "/api" and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # 回覆統一禁止快取，包含中介層錯誤、路由錯誤與 Session 成功回覆。
        async def api_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        # 保持拒絕回覆固定，不回顯來源、本文或外部標頭。
        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(
                scope, receive, api_send
            )

        # 在判斷同源前先驗證唯一的 Host，並拒絕不合法的連接埠。
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        host = HOST_FORMAT.fullmatch(hosts[0]) if len(hosts) == 1 else None
        if (
            host is None
            or host[1].lower() not in LOCAL_HOSTS
            or (host[2] is not None and int(host[2]) > 65535)
        ):
            await reject(400, "主機標頭不正確。")
            return

        # Session 查詢會刷新閒置時間，因此與其他異動共用來源防護。
        method = scope["method"]
        session_refresh = method == "GET" and path == "/api/session"
        if session_refresh or method not in ("GET", "HEAD", "OPTIONS"):
            scheme = scope.get("scheme", "http")
            port = host[2]
            if port is None or (scheme, port) in (("http", "80"), ("https", "443")):
                port = ""
            else:
                port = f":{port}"

            # 同源 GET 可能沒有 Origin，但仍須帶自訂標頭；其他異動須明確帶 Origin。
            origin = f"{scheme}://{host[1].lower()}{port}"
            origins = headers.getlist("origin")
            can_omit_origin = session_refresh and not origins
            valid_origin = origins == [origin] or can_omit_origin
            if not valid_origin or headers.getlist("x-csrf-protection") != ["1"]:
                await reject(403, "請從工作台重新發送此請求。")
                return

        # 先拒絕矛盾、畸形或宣告過大的長度，避免解析或等待大型本文。
        lengths = headers.getlist("content-length")
        declared = None
        if lengths:
            length = lengths[0]
            if (
                len(lengths) != 1
                or "transfer-encoding" in headers
                or not (length.isascii() and length.isdecimal())
            ):
                await reject(400, "請求本文長度不正確。")
                return
            length = length.lstrip("0") or "0"
            if len(length) > 5 or int(length) > BODY_LIMIT:
                await reject(413, "請求本文不得超過 64 KiB。")
                return
            declared = int(length)

        # 不信任宣告長度；對未標長度及分段傳輸也只累積至允許上限。
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > BODY_LIMIT:
                await reject(413, "請求本文不得超過 64 KiB。")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if declared is not None and len(body) != declared:
            await reject(400, "請求本文長度不正確。")
            return

        # 重放有界本文一次；後續讀取保留伺服器提供的中斷通知。
        replayed = False

        # 讓下游使用標準 Request 介面讀取完整資料。
        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, api_send)
