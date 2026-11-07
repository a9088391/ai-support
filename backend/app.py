from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint

from backend import knowledge, sessions
from backend.activation import setup_status
from backend.credentials import CredentialError
from backend.database import Settings, initialize
from backend.http_security import LOCAL_HOSTS, ApiRequestMiddleware

STATIC = Path(__file__).parents[1] / "public"
LOGIN_INVALID = "登入欄位格式不正確。"
LOGIN_DENIED = "帳號或密碼不正確。"
SEARCH_INVALID = "搜尋欄位格式不正確。"


# 同一個來源用同一個標準 IP 表示；來源只取 ASGI 對端，無法以 HTTP 標頭改寫。
def login_source(request: Request) -> str:
    try:
        return str(ip_address(request.client.host)) if request.client else "unknown"
    except ValueError:
        return "unknown"


# 建立單一同源服務；設定可由隔離測試明確傳入，正式環境從後端掛載讀取。
def create_app(settings: Settings | None = None, static: Path = STATIC) -> FastAPI:
    @asynccontextmanager
    # 啟動只初始化資料庫，不在啟動或重啟時呼叫模型。
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            # 初始化放在工作執行緒，資料庫尚未就緒時拒絕接受流量。
            current = settings or Settings.from_environment()
            await run_in_threadpool(initialize, current)
            await run_in_threadpool(sessions.initialize_accounts, current)
            app.state.settings = current
        except CredentialError as error:
            raise RuntimeError(str(error)) from None
        except psycopg.Error:
            raise RuntimeError(
                "無法初始化本機資料庫，請檢查專案服務及本機設定。"
            ) from None
        yield

    # 停用框架的公開開發文件路由，避免和產品同源頁面混用。
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    # 統一攔截 API 入口，讓未啟用狀態連直接請求也無法使用業務功能。
    async def require_activation(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 登出即使在啟用失效後仍須撤銷；其他 API 先核對目前設定。
        path = request.scope["path"]
        is_api = path == "/api" or path.startswith("/api/")
        logout = path == "/api/logout" and request.method == "POST"
        if is_api and not logout:
            try:
                status = await run_in_threadpool(
                    setup_status, request.app.state.settings
                )
            except psycopg.Error:
                return JSONResponse(
                    {"detail": "目前無法確認啟用狀態。"},
                    status_code=503,
                )
            if path == "/api/setup" and request.method == "GET":
                return JSONResponse(status.model_dump())
            if status.status != "ready":
                return JSONResponse({"detail": status.message}, status_code=503)

        # 通過門檻後才交給 API 或同源靜態路由。
        return await call_next(request)

    @app.exception_handler(psycopg.Error)
    # 登入、刷新或撤銷遇到資料庫錯誤時一律安全失敗，不外露診斷細節。
    async def database_failure(_: Request, _error: Exception) -> JSONResponse:
        return JSONResponse({"detail": "目前無法確認登入狀態。"}, status_code=503)

    @app.get("/api/session")
    # 從受保護的 Cookie 確認身分；被動查詢可保留原閒置時間。
    def session_status(request: Request) -> sessions.Identity:
        # 缺少參數時沿用刷新行為；拒絕模稜兩可的值且不回顯原始輸入。
        refresh = request.query_params.getlist("refresh")
        if refresh not in ([], ["true"], ["false"]):
            raise HTTPException(status_code=422, detail="工作階段查詢參數格式不正確。")

        # 兩種查詢都核對期限及撤銷狀態，只有主動查詢可刷新有效工作階段。
        identity = sessions.active_session(
            request.app.state.settings,
            request.cookies.get(sessions.COOKIE_NAME),
            refresh=refresh != ["false"],
        )
        if identity is None:
            raise HTTPException(status_code=401, detail="請先登入。")
        return identity

    @app.post("/api/login")
    # 驗證有界的 JSON 後換發 Cookie，回應只含工作台所需的身分欄位。
    async def login(request: Request, response: Response) -> sessions.Identity:
        # 自行處理欄位錯誤，避免框架回傳原始秘密、未知欄位名稱或錯誤位置。
        types = request.headers.getlist("content-type")
        if (
            len(types) != 1
            or types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise HTTPException(status_code=422, detail=LOGIN_INVALID)
        try:
            form = sessions.LoginInput.model_validate_json(await request.body())
        except ValidationError:
            raise HTTPException(status_code=422, detail=LOGIN_INVALID) from None
        username = form.username.strip()
        if sessions.USERNAME_FORMAT.fullmatch(username) is None:
            raise HTTPException(status_code=422, detail=LOGIN_INVALID)

        # 雜湊與 PostgreSQL 交易均在工作執行緒；未知帳號與錯誤密碼共用回覆。
        try:
            result = await run_in_threadpool(
                sessions.login,
                request.app.state.settings,
                username.lower(),
                form.password,
                login_source(request),
                request.cookies.get(sessions.COOKIE_NAME),
            )
        except sessions.LoginLimited as error:
            raise HTTPException(
                status_code=429,
                detail="登入嘗試過多，請稍後再試。",
                headers={"Retry-After": str(error.retry_after)},
            ) from None
        if result is None:
            raise HTTPException(status_code=401, detail=LOGIN_DENIED)

        # 目前服務只綁 loopback HTTP；Cookie 不指定 Domain，保留主機限定範圍。
        identity, token = result
        response.set_cookie(
            sessions.COOKIE_NAME,
            token,
            max_age=8 * 60 * 60,
            httponly=True,
            secure=False,
            samesite="lax",
            path="/",
        )
        return identity

    @app.post("/api/logout", status_code=204)
    # 伺服器完成撤銷後才清除瀏覽器 Cookie；沒有登入也可重送。
    def logout(request: Request) -> Response:
        sessions.logout(
            request.app.state.settings, request.cookies.get(sessions.COOKIE_NAME)
        )
        response = Response(status_code=204)
        response.delete_cookie(
            sessions.COOKIE_NAME,
            path="/",
            httponly=True,
            secure=False,
            samesite="lax",
        )
        return response

    @app.post("/api/knowledge/search")
    # 搜尋只接受有界的同源 JSON；沿用啟用與工作階段驗證，不呼叫模型。
    async def search_knowledge(request: Request) -> knowledge.SearchResponse:
        # 中介層已限制本文大小；自行解析，避免格式錯誤回顯查詢內容。
        types = request.headers.getlist("content-type")
        if (
            len(types) != 1
            or types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise HTTPException(status_code=422, detail=SEARCH_INVALID)
        try:
            form = knowledge.SearchInput.model_validate(
                knowledge.parse_json(await request.body())
            )
        except (ValueError, RecursionError):
            raise HTTPException(status_code=422, detail=SEARCH_INVALID) from None

        # 列鎖與資料查詢放到工作執行緒；等交易完成後才回覆結果或拒絕原因。
        try:
            results = await run_in_threadpool(
                knowledge.search,
                request.app.state.settings,
                request.cookies.get(sessions.COOKIE_NAME),
                form.query.strip(),
            )
        except knowledge.ActivationRequired as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except psycopg.Error:
            raise HTTPException(
                status_code=503, detail="目前無法搜尋知識文件。"
            ) from None
        if results is None:
            raise HTTPException(status_code=401, detail="請先登入。")
        return results

    @app.get("/health")
    # 提供不會接觸模型的本機存活檢查。
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/{path:path}")
    # 同源提供 Nuxt 的靜態輸出；未定義的 API 保留 JSON 404。
    def frontend(path: str) -> FileResponse:
        # API 一律不讀取前端輸出，即使檔案與路由同名。
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)

        # 只提供輸出根目錄下的實際檔案，拒絕路徑穿越與指向外部的連結。
        root = static.resolve()
        target = (root / (path or "index.html")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(target)

    # API 先檢查 Host、來源及本文，既有 Host 保護繼續涵蓋靜態頁與健康檢查。
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.add_middleware(ApiRequestMiddleware)
    return app


app = create_app()
