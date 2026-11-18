import json
from pathlib import Path

import pytest
from backend import knowledge
from backend.database import Settings
from backend.tests.test_knowledge import import_batch, search
from backend.tests.test_sessions import CSRF, USER_FORM
from backend.tests.test_sessions import client as client
from backend.tests.test_sessions import clock as clock
from backend.tests.test_sessions import ready as ready
from fastapi.testclient import TestClient

CORPUS = Path(__file__).resolve().parents[2] / "knowledge" / "documents.json"
REVISIONS = {
    "taiwan": 95266943,
    "taipei": 95367331,
    "tainan": 95266937,
    "yushan": 95160511,
    "sun-moon-lake": 94010805,
    "alishan": 91942698,
    "national-palace-museum": 95384135,
    "taiwan-high-speed-rail": 94426625,
    "zengwen-reservoir": 93980152,
}
STABLE = {
    "taiwan": ("location", "name"),
    "taipei": ("location", "districts"),
    "tainan": ("history", "old-town"),
    "yushan": ("elevation", "national-park"),
    "sun-moon-lake": ("location", "island"),
    "alishan": ("location", "sights"),
    "national-palace-museum": ("locations", "history"),
    "taiwan-high-speed-rail": ("route", "service"),
}


# 公開語料必須通過正式 DTO，且每段都可追溯固定修訂、作者、改作與授權。
def test_corpus_has_broad_attributed_source_paragraphs() -> None:
    batch = knowledge.parse_batch(CORPUS.read_bytes())
    assert {document.id for document in batch.documents} == REVISIONS.keys()
    for document in batch.documents:
        # 更新正文保留穩定識別並換版，避免工單與現行引用混淆。
        revision = REVISIONS[document.id]
        assert document.version == f"zhwiki-r{revision}-body-v2"
        assert set(STABLE.get(document.id, ())) <= {f.id for f in document.fragments}
        assert 5 <= len(document.fragments) <= 100
        body_length = 0
        for fragment in document.fragments:
            # 歸屬寫在實際引用文字中，不能只依賴額外網頁或匯入者記憶。
            text = fragment.text
            body, attribution = text.split("\n\n來源：", 1)
            body_length += len(body)
            assert "中文維基百科" in attribution
            assert "貢獻者" in attribution and "修改：" in attribution
            assert (
                f"https://zh.wikipedia.org/w/index.php?oldid={revision}" in attribution
            )
            assert (
                "CC BY-SA 4.0 https://creativecommons.org/licenses/by-sa/4.0/" in text
            )
            assert len(text) <= 4000
            assert "<script" not in text and ".mw-parser-output" not in text
            assert "坐標：參數：" not in text
        assert body_length > 2500


@pytest.mark.parametrize(
    ("query", "document_id", "fact"),
    [
        ("臺灣最高峰是？有多高？", "yushan", "3,952"),
        (
            "臺灣最大的湖是哪一個？最大的水庫是哪一個？",
            "zengwen-reservoir",
            "庫容量最大",
        ),
        ("臺灣最大的湖是哪一個？最大的水庫是哪一個？", "sun-moon-lake", "天然湖泊"),
        ("最大的水庫是哪一個？", "zengwen-reservoir", "庫容量最大"),
        ("西方國家在歷史上稱台灣為什麼？", "taiwan", "福爾摩沙"),
        ("臺灣的面積多大", "taiwan", "35,886.8623"),
        ("台灣面積", "taiwan", "35,886.8623"),
        ("福爾摩沙", "taiwan", "Formosa"),
        ("Ｆｏｒｍｏｓａ", "taiwan", "美麗島"),
        ("湖 水庫", "zengwen-reservoir", "水庫"),
        ("台北有哪些行政區？", "taipei", "中正"),
        ("台南為什麼稱為府城？", "tainan", "臺灣府"),
        ("高鐵有幾個車站？起點終點是哪裡？", "taiwan-high-speed-rail", "12"),
        ("故宮南北院所在地", "national-palace-museum", "太保市"),
        ("阿里山有哪些景點？", "alishan", "姐妹潭"),
        ("阿里山五奇有哪些？", "alishan", "晚霞"),
        ("日月潭的發電系統", "sun-moon-lake", "大觀發電廠"),
        ("臺北市的市花", "taipei", "杜鵑花"),
        ("曾文水庫的壩高", "zengwen-reservoir", "133公尺"),
        ("玉山的植物生態", "yushan", "植物"),
    ],
)
# 經正式 CLI、HTTP 與真實 PG 驗證原有事實皆在前八段，不因模型來源預算遺失答案。
def test_corpus_natural_questions_retrieve_real_evidence(
    client: TestClient,
    ready: Settings,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    document_id: str,
    fact: str,
) -> None:
    # 走同一登入與搜尋入口，來源及片段必須確實來自當前匯入版本。
    assert import_batch(monkeypatch, CORPUS.read_bytes()) == 0
    assert client.post("/api/login", headers=CSRF, json=USER_FORM).status_code == 200
    response = search(client, query)
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) <= knowledge.RESULT_LIMIT
    assert any(
        result["document_id"] == document_id and fact in result["excerpt"]
        for result in results[:8]
    ), query
    actual = json.loads(CORPUS.read_bytes())
    for result in results:
        document = next(
            d for d in actual["documents"] if d["id"] == result["document_id"]
        )
        fragment = next(
            f for f in document["fragments"] if f["id"] == result["fragment_id"]
        )
        assert result["excerpt"] == fragment["text"]
        assert result["version"] == document["version"]
