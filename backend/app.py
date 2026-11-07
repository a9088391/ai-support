from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint

from backend.activation import setup_status
from backend.credentials import CredentialError
from backend.database import Settings, initialize

STATIC = Path(__file__).parents[1] / "public"
NO_STORE = {"Cache-Control": "no-store"}


# 建立單一同源服務；設定可由隔離測試明確傳入，正式環境從後端掛載讀取。
def create_app(settings: Settings | None = None, static: Path = STATIC) -> FastAPI:
    @asynccontextmanager
    # 啟動只初始化資料庫，不在啟動或重啟時呼叫模型。
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            # 初始化放在工作執行緒，資料庫尚未就緒時拒絕接受流量。
            current = settings or Settings.from_environment()
            await run_in_threadpool(initialize, current)
            app.state.settings = current
        except (CredentialError, psycopg.Error):
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
        # 只放行讀取狀態、健康檢查與靜態資源；其他 API 先核對目前設定。
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            try:
                status = await run_in_threadpool(
                    setup_status, request.app.state.settings
                )
            except psycopg.Error:
                return JSONResponse(
                    {"detail": "目前無法確認啟用狀態。"},
                    status_code=503,
                    headers=NO_STORE,
                )
            if path == "/api/setup" and request.method == "GET":
                return JSONResponse(status.model_dump(), headers=NO_STORE)
            if status.status != "ready":
                return JSONResponse(
                    {"detail": status.message}, status_code=503, headers=NO_STORE
                )

        # 通過門檻後才交給 API 或同源靜態路由。
        return await call_next(request)

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

    # 將 Host 檢查放在最外層，包含直接回覆的狀態與拒絕請求。
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    return app


app = create_app()
