import json
import re
from typing import Annotated

from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from backend import sessions
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


class Fragment(KnowledgeInput):
    id: Identifier
    text: PlainText = Field(max_length=4000)


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
            Jsonb([fragment.model_dump() for fragment in document.fragments]),
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

        # 以單一 SELECT 取得完整舊批或新批。C collation 只折疊 ASCII 大小寫，
        # strpos 不把百分號或底線當成樣式；依穩定來源識別排序後取有界結果。
        with conn.cursor(row_factory=class_row(SearchResult)) as cursor:
            cursor.execute(
                """
                SELECT d.id AS document_id, d.title, d.version,
                       f.id AS fragment_id, f.text AS excerpt
                FROM knowledge_documents AS d
                CROSS JOIN LATERAL jsonb_to_recordset(d.fragments) AS f(id text, text text)
                CROSS JOIN (SELECT lower(%s::text COLLATE "C") AS term) AS q
                WHERE strpos(lower(d.title COLLATE "C"), q.term) > 0
                   OR strpos(lower(f.text COLLATE "C"), q.term) > 0
                ORDER BY d.id COLLATE "C", f.id COLLATE "C"
                LIMIT %s
                """,
                (query, RESULT_LIMIT),
            )
            return SearchResponse(results=cursor.fetchall())
