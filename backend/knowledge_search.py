import math
import re
import unicodedata
from collections import Counter

HAN = r"[\u3400-\u9fff]"
QUESTION_WORDS = re.compile(
    "為什麼|是什麼|哪一個|哪個|有哪些|什麼|多少|多大|多高|如何|怎麼|請問|"
    "告訴我|可以|是否|幾個|幾座|哪裡|何時|的|了|嗎|呢|在|是|有|與|和|及|為"
)


# 只改比對形式，回傳與引用仍使用資料庫原文；臺台與全形字不需改寫來源。
def normalize(text: str) -> str:
    # 依既有順序統一字寬、大小寫及臺台，再合併空白供後續比對。
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("臺", "台")
    return re.sub(r"\s+", " ", normalized).strip()


# 中文用雙字保留主題訊號，英文與代碼維持完整詞，避免模糊命中錯誤識別。
def terms(query: str) -> set[str]:
    # 問句虛詞只切開中文；含百分號與底線的代碼仍是完整字面詞。
    parts = re.findall(r"[a-z0-9][a-z0-9_%.-]*|[\u3400-\u9fff]+|[%_]+", query)
    result: set[str] = set()
    for part in parts:
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            for word in QUESTION_WORDS.split(part):
                if len(word) > 1:
                    result.update(word[n : n + 2] for n in range(len(word) - 1))
                elif word:
                    result.add(word)
        else:
            result.add(part)
    return result


# shortcut: 針對本機有限語料逐片比對；資料量超出現行匯入上限時再評估索引。
def rank(query: str, candidates: list[tuple[str, str]]) -> list[int]:
    # 先正規化與切詞；純問句虛詞不應召回整份知識庫。
    query = normalize(query)
    keywords = terms(query)
    if not keywords:
        return []
    normalized = [(normalize(title), normalize(text)) for title, text in candidates]
    matches = [
        {term for term in keywords if term in title or term in text}
        for title, text in normalized
    ]

    # 常見字詞降低權重，避免「臺灣」壓過問題真正要查的地理或文化主題。
    frequency = Counter(term for found in matches for term in found)
    weights = {
        term: 1 + math.log((1 + len(candidates)) / (1 + frequency[term]))
        for term in sorted(keywords)
    }
    scored: list[tuple[float, bool, int]] = []
    for index, ((title, text), found) in enumerate(zip(normalized, matches)):
        # 短識別與符號維持字面比對；中文需足夠重疊，或命中稀有的標題名稱。
        exact = query in title or query in text
        named = {
            term
            for term in found
            if len(term) >= 2
            and term in title
            and frequency[term] < len(candidates) / 4
        }
        if not re.search(HAN, query) and not exact:
            continue
        if (
            not exact
            and not named
            and (len(found) < min(2, len(keywords)) or len(found) / len(keywords) < 0.4)
        ):
            continue
        if not found:
            continue

        # 保留原相關性分數；完整文件標題只在同分時優先，不壓過更相關的內容。
        score = sum(weights[term] for term in sorted(found))
        score += sum(weights[term] for term in sorted(named)) * 2
        score *= 1 + len(found) / len(keywords)
        if exact:
            score += sum(weights.values()) * 2
        scored.append((score, title in query, index))

    # 分數與標題條件均相同時，沿用資料庫的穩定來源順序。
    return [
        index
        for _, _, index in sorted(
            scored, key=lambda item: (-item[0], not item[1], item[2])
        )
    ]
