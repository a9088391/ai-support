import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from backend import knowledge_search, sessions
from backend.activation import setup_status
from backend.database import Settings

IMPORT_LIMIT = 4 * 1024 * 1024
RESULT_LIMIT = 20
INVALID_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
Identifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
]


# 在進入資料庫前拒絕空白與無效碼位；保留來源片段本來的空白和換行。
def valid_text(value: str) -> str:
    if not value.strip() or INVALID_CHAR.search(value):
        raise ValueError("需提供有效的純文字。")
    return value


PlainText = Annotated[str, AfterValidator(valid_text)]


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class ApplicationRecipient(KnowledgeInput):
    role: Identifier
    label: PlainText = Field(max_length=200)


class Permission(KnowledgeInput):
    role: Identifier
    action: Identifier
    resource: Identifier
    effect: Literal["allow", "deny"]
    application_recipient: ApplicationRecipient | None = None

    @model_validator(mode="after")
    # 申請對象只補充不能自行操作的事實，避免允許與申請限制互相矛盾。
    def recipient_requires_denial(self) -> "Permission":
        if self.application_recipient is not None and self.effect != "deny":
            raise ValueError("只有禁止操作的權限事實可列申請對象。")
        return self


class Fragment(KnowledgeInput):
    id: Identifier
    text: PlainText = Field(max_length=4000)
    permissions: list[Permission] | None = Field(default=None, max_length=100)


class Document(KnowledgeInput):
    id: Identifier
    title: PlainText = Field(max_length=200)
    version: PlainText = Field(max_length=64)
    fragments: list[Fragment] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    # 同一文件的來源位置不能有兩個相同識別。
    def unique_fragments(self) -> "Document":
        if len({fragment.id for fragment in self.fragments}) != len(self.fragments):
            raise ValueError("同一文件的片段識別不可重複。")
        return self


class Batch(KnowledgeInput):
    documents: list[Document] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    # 一批內每份文件只允許出現一次，避免以輸入順序決定生效內容。
    def unique_documents(self) -> "Batch":
        if len({document.id for document in self.documents}) != len(self.documents):
            raise ValueError("文件識別不可重複。")
        return self


class SearchInput(KnowledgeInput):
    query: PlainText = Field(max_length=2000)


class SearchResult(BaseModel):
    document_id: str
    title: str
    version: str
    fragment_id: str
    excerpt: str


@dataclass(frozen=True)
class SearchEvidence:
    source: SearchResult
    permissions: tuple[Permission, ...]


class SearchResponse(BaseModel):
    results: list[SearchResult]


class ActivationRequired(Exception):
    pass


# JSON 欄位不得重複，避免解析時靜默覆蓋前面的值。
def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("JSON 欄位不可重複。")
    return fields


# CLI 與 HTTP 皆從明確的 UTF-8 本文解析，拒絕同名欄位再交給 DTO。
def parse_json(body: bytes) -> object:
    return json.loads(body.decode("utf-8"), object_pairs_hook=unique_fields)


# 先限制匯入本文大小，再驗證完整批次；呼叫端只會得到可寫入的資料。
def parse_batch(body: bytes) -> Batch:
    if len(body) > IMPORT_LIMIT:
        raise ValueError("知識文件匯入本文超過上限。")
    return Batch.model_validate(parse_json(body))


# 只替換專用知識表；失敗時連刪除也回滾，搜尋始終讀到已提交的完整批次。
def replace_documents(settings: Settings, batch: Batch) -> None:
    # 在持鎖前準備整批資料，減少同時執行匯入時的等待。
    rows = [
        (
            document.id,
            document.title,
            document.version,
            Jsonb(
                [
                    fragment.model_dump(exclude_none=True)
                    for fragment in document.fragments
                ]
            ),
        )
        for document in batch.documents
    ]
    with settings.connect() as conn, conn.cursor() as cursor:
        # 先序列化寫入者再刪除；這個鎖與普通 SELECT 相容，讀者可繼續讀舊快照。
        cursor.execute("LOCK TABLE knowledge_documents IN SHARE ROW EXCLUSIVE MODE")
        cursor.execute("DELETE FROM knowledge_documents")
        cursor.executemany(
            """
            INSERT INTO knowledge_documents (id, title, version, fragments)
            VALUES (%s, %s, %s, %s)
            """,
            rows,
        )


# 同一交易確認登入和啟用後才讀取文件；列鎖讓登出及啟用異動與讀取有明確順序。
def search(settings: Settings, token: str | None, query: str) -> SearchResponse | None:
    # 明顯不合格式的 Cookie 不必連線。
    digest = sessions.token_digest(token)
    if digest is None:
        return None

    with settings.connect() as conn:
        # 先鎖住有效的 Session，確認存在且未到期，此時不刷新活動時間。
        if sessions.read_active_session(conn, digest, refresh=False) is None:
            return None

        # 等到拿到 Session 鎖後才重查啟用；失效時不讀文件或刷新活動時間。
        status = setup_status(settings, lock_with=conn)
        if status.status != "ready":
            raise ActivationRequired(status.message)

        # 等待啟用列鎖後重新判斷期限，有效才刷新；到期則提交刪除。
        if sessions.read_active_session(conn, digest, refresh=True) is None:
            return None

        # 共用相同的字詞、排序與資料庫快照規則。
        return SearchResponse(results=find_fragments(conn, query))


# 單一 SELECT 讀取已提交的完整批次；呼叫端先在自己的交易驗證存取權。
def find_fragments(
    conn: Connection[tuple[object, ...]], query: str
) -> list[SearchResult]:
    return [evidence.source for evidence in find_evidence(conn, query)]


# 權限事實和來源文字由同一 SELECT 擷取；內部資料不進公開 DTO 或模型交換。
def find_evidence(
    conn: Connection[tuple[object, ...]], query: str
) -> list[SearchEvidence]:
    # 以單一 SELECT 取得完整舊批或新批，再對有限本機語料排名；不先截掉相關來源。
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT d.id AS document_id, d.title, d.version,
                   f.id AS fragment_id, f.text AS excerpt, f.permissions
            FROM knowledge_documents AS d
            CROSS JOIN LATERAL jsonb_to_recordset(d.fragments) AS f(id text, text text, permissions jsonb)
            ORDER BY d.id COLLATE "C", f.id COLLATE "C"
            """,
        )
        rows = cursor.fetchall()

    # 手動搜尋與模型工具共用相同排名，最後才限制公開結果數量。
    ranked = knowledge_search.rank(
        query, [(row["title"], row["excerpt"]) for row in rows]
    )

    # 保留可序列化的公開來源與不外傳的權限事實，舊文件缺欄時視為未列明。
    return [
        SearchEvidence(
            source=SearchResult.model_validate(row),
            permissions=tuple(
                Permission.model_validate(permission)
                for permission in (row["permissions"] or [])
            ),
        )
        for index in ranked[:RESULT_LIMIT]
        for row in [rows[index]]
    ]
