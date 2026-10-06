"""Local, rule-based classification and usefulness scoring of comments.

* Classifies the content of a comment, never the person who wrote it.
* Uses no external service and no trained model; nothing is learned from
  Reddit content.
* Reddit text is untrusted data: it is only matched against fixed patterns.
  Text that looks like instructions to software/AI is flagged, never followed.
* Keeps three things apart: the original text (stored as is), the system
  summary (extractive, labelled as such) and the system assessment (scores and
  reasons).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from .config import Config
from .storage import Store, now_iso, parse_iso

CATEGORY_LABELS = {
    "instruction": "Инструкция",
    "practical_advice": "Практический совет",
    "personal_experience": "Личный опыт",
    "resource_link": "Ссылка на ресурс",
    "reasoned_objection": "Аргументированное возражение",
    "question": "Вопрос",
    "discussion": "Обсуждение без практического вывода",
    "promo_spam": "Реклама или спам",
}
NOT_SELECTABLE = {"question", "discussion", "promo_spam"}
CRITERIA_LABELS = {
    "applicability": "практическая применимость",
    "specificity": "конкретность",
    "evidence": "обоснования и источники",
    "originality": "оригинальность",
    "interest": "соответствие интересам",
    "votes": "голоса Reddit (вспомогательный сигнал)",
}


def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(words) + r")(?!\w)", re.IGNORECASE)


URL_RX = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
STEP_RX = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•]|step \d+|шаг \d+)\s+\S", re.IGNORECASE | re.MULTILINE)
SEQ_RX = _rx(["first(?:ly)?", "then", "next", "after that", "finally", "сначала", "затем", "потом", "после этого", "наконец"])
ADVICE_RX = _rx([
    "you should", "you could", "try", "i recommend", "i'd recommend", "i would recommend", "i suggest",
    "i'd suggest", "make sure", "don't", "do not", "avoid", "use", "consider", "the trick is", "pro tip",
    "tip", "best to", "it helps to", "recommend", "always", "never",
    "советую", "рекомендую", "попробуйте", "попробуй", "стоит", "не стоит", "используйте", "лучше",
    "обязательно", "избегайте", "не надо", "нужно", "можно",
])
EXPERIENCE_RX = _rx([
    "i", "i've", "i'm", "i had", "my", "me", "we", "our", "in my experience", "when i", "i tried",
    "я", "мой", "моя", "мои", "у меня", "мы", "когда я", "по моему опыту", "я пробовал", "я пробовала",
])
QUESTION_START_RX = re.compile(
    r"^\s*(?:what|how|why|when|where|which|who|does|do|is|are|can|could|should|would|anyone|"
    r"что|как|почему|зачем|когда|где|какой|какая|какие|кто|можно ли|есть ли|подскажите)\b",
    re.IGNORECASE,
)
OBJECTION_RX = _rx([
    "but", "however", "disagree", "not true", "actually", "i don't think", "on the other hand",
    "that's wrong", "that is wrong", "not necessarily", "myth", "misconception", "careful",
    "но", "однако", "не согласен", "не согласна", "на самом деле", "это не так", "миф", "неверно",
])
REASON_RX = _rx([
    "because", "since", "due to", "the reason", "which means", "so that", "therefore",
    "потому что", "так как", "из-за", "поэтому", "следовательно", "поскольку",
])
SOURCE_RX = _rx([
    "source", "sources", "study", "studies", "research", "paper", "according to", "data", "documentation",
    "docs", "manual", "official", "measured", "tested",
    "источник", "исследование", "исследования", "по данным", "документация", "инструкция производителя",
    "измерил", "проверил",
])
SPAM_RX = _rx([
    "discount", "promo code", "coupon", "use my link", "use my code", "affiliate", "dm me", "check out my",
    "buy now", "subscribe", "free trial", "limited offer", "click here", "follow me", "giveaway",
    "скидка", "промокод", "купон", "пишите в личку", "подписывайтесь", "переходи по ссылке", "акция",
])
TRACKING_RX = re.compile(r"[?&](?:ref|aff|affiliate|utm_[a-z]+|tag)=", re.IGNORECASE)
UNIT_NUM_RX = re.compile(
    r"\d+(?:[.,]\d+)?\s?(?:%|°[cf]?|mg|g|kg|lb|lbs|oz|ml|l|cm|mm|m|km|in|ft|h|hr|hrs|hours?|min|mins|minutes?|"
    r"sec|s|days?|weeks?|months?|years?|\$|€|usd|eur|rpm|w|kw|v|mah|gb|tb|мг|г|кг|мл|л|см|мм|м|км|ч|час(?:а|ов)?|"
    r"мин(?:ут)?|сек|дн(?:я|ей)?|недел[ьи]|месяц(?:а|ев)?|лет|год(?:а)?|руб|₽)(?!\w)",
    re.IGNORECASE,
)
NUM_RX = re.compile(r"\d+(?:[.,]\d+)?")
LOW_EFFORT_RX = re.compile(
    r"^\s*(?:this|same|\+1|lol|lmao|thanks?|thank you|agreed|yes|no|this\.?|so true|"
    r"спасибо|согласен|согласна|да|нет|плюсую|\+)\s*[.!]*\s*$",
    re.IGNORECASE,
)
INJECTION_RX = re.compile(
    r"(ignore (?:all |any )?(?:previous|prior|above) instructions|system prompt|you are (?:now )?an? (?:ai|assistant)|"
    r"as an ai|run (?:this|the following) command|execute|rm -rf|<script|игнорируй (?:все )?(?:предыдущие )?инструкции)",
    re.IGNORECASE,
)
SENT_SPLIT_RX = re.compile(r"(?<=[.!?…])\s+|\n+")
WORD_RX = re.compile(r"\w+", re.UNICODE)


@dataclass
class Assessment:
    comment_id: str
    post_id: str
    primary_category: str
    categories: list[str]
    scores: dict[str, float]
    total: float
    selected: bool
    reasons: list[str]
    summary: str
    topics: list[str]
    flags: list[str] = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT_RX.split(text) if len(s.strip()) > 2]


def _shingles(text: str, n: int = 3) -> set[tuple]:
    words = [w.lower() for w in WORD_RX.findall(text)]
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _interest_hits(cfg: Config, text: str) -> dict[str, float]:
    low = text.lower()
    hits = {}
    for it in cfg.interests:
        n = 0
        for kw in it.keywords:
            if " " in kw or not kw.isalnum():
                n += low.count(kw)
            else:
                n += len(re.findall(r"(?<!\w)" + re.escape(kw) + r"\w{0,3}(?!\w)", low))
        if n:
            hits[it.name] = n * it.weight
    return hits


def _key_sentence(sentences: list[str]) -> str:
    best, best_score = "", -1.0
    for s in sentences:
        score = (2 * len(ADVICE_RX.findall(s)) + len(UNIT_NUM_RX.findall(s)) + len(REASON_RX.findall(s))
                 + (1 if 40 <= len(s) <= 220 else 0) - (2 if s.endswith("?") else 0))
        if score > best_score:
            best, best_score = s, score
    if len(best) > 220:
        best = best[:217].rstrip() + "…"
    return best


def assess_comment(
    cfg: Config,
    comment: dict,
    *,
    post_title: str,
    earlier_shingles: list[tuple[str, set]],
) -> Assessment:
    body: str = comment["body"]
    text_wo_urls = URL_RX.sub(" ", body)
    sentences = _sentences(text_wo_urls)
    words = WORD_RX.findall(text_wo_urls)
    n_words = max(1, len(words))

    urls = URL_RX.findall(body)
    steps = len(STEP_RX.findall(body))
    seq = len(SEQ_RX.findall(text_wo_urls))
    advice = len(ADVICE_RX.findall(text_wo_urls))
    experience = len(EXPERIENCE_RX.findall(text_wo_urls))
    objection = len(OBJECTION_RX.findall(text_wo_urls))
    reasons_n = len(REASON_RX.findall(text_wo_urls))
    source_words = len(SOURCE_RX.findall(text_wo_urls))
    spam = len(SPAM_RX.findall(body)) + len(TRACKING_RX.findall(body))
    unit_nums = len(UNIT_NUM_RX.findall(text_wo_urls))
    nums = len(NUM_RX.findall(text_wo_urls))
    q_sent = sum(1 for s in sentences if s.endswith("?") or QUESTION_START_RX.match(s))
    q_ratio = q_sent / max(1, len(sentences))
    is_reply = comment["parent_id"].startswith("t1_")
    flags = ["содержит текст, похожий на команды для ПО/ИИ; обработан только как данные"] if INJECTION_RX.search(body) else []

    # ---- categories (content only) ---------------------------------------
    cat: dict[str, float] = {}
    link_ratio = len(urls) / n_words
    cat["promo_spam"] = _clip(0.45 * spam + (0.4 if link_ratio > 0.2 and len(urls) >= 2 else 0))
    cat["instruction"] = _clip(0.3 * steps + 0.15 * seq + (0.2 if steps and advice else 0))
    cat["practical_advice"] = _clip(0.25 * advice + 0.1 * unit_nums - 0.4 * q_ratio)
    cat["personal_experience"] = _clip(0.4 * experience / max(1.0, n_words / 20) - 0.3 * q_ratio)
    cat["resource_link"] = _clip(0.6 * len(urls) - 0.3 * spam)
    cat["reasoned_objection"] = _clip((0.45 * objection) * (1 if (reasons_n or urls or source_words) else 0.3)
                                      + (0.15 if is_reply and objection else 0) + 0.1 * reasons_n)
    cat["question"] = _clip(q_ratio * 1.1 - 0.15 * advice)
    if cat["promo_spam"] >= 0.6:
        for k in cat:
            if k != "promo_spam":
                cat[k] *= 0.3
    best = max(cat, key=cat.get)
    primary = best if cat[best] >= 0.35 else "discussion"
    categories = [k for k, v in sorted(cat.items(), key=lambda kv: -kv[1]) if v >= 0.35] or ["discussion"]
    if primary not in categories:
        categories.insert(0, primary)

    # ---- criteria --------------------------------------------------------
    base_app = {"instruction": 0.9, "practical_advice": 0.8, "resource_link": 0.55, "personal_experience": 0.55,
                "reasoned_objection": 0.55, "question": 0.1, "discussion": 0.15, "promo_spam": 0.0}[primary]
    applicability = _clip(base_app + 0.05 * min(advice, 4) + 0.05 * min(steps, 3))
    length_factor = _clip(math.log10(n_words + 1) / 2.3)  # ~200 words -> 1.0
    specificity = _clip(0.12 * unit_nums + 0.03 * max(0, nums - unit_nums) + 0.08 * steps + 0.4 * length_factor)
    evidence = _clip(0.3 * min(reasons_n, 2) + 0.3 * min(len(urls), 2) * (0 if spam else 1) + 0.25 * min(source_words, 2))

    shingles = _shingles(text_wo_urls)
    max_sim, similar_to = 0.0, None
    for other_id, other in earlier_shingles:
        if shingles and other:
            sim = len(shingles & other) / len(shingles | other)
            if sim > max_sim:
                max_sim, similar_to = sim, other_id
    low_effort = bool(LOW_EFFORT_RX.match(body)) or n_words < 6
    originality = _clip((1 - max_sim) * (0.3 if low_effort else 1.0) * (0.5 + 0.5 * length_factor))

    own_hits = _interest_hits(cfg, body)
    title_hits = _interest_hits(cfg, post_title)
    if cfg.interests:
        interest = _clip(0.35 * sum(own_hits.values()) + 0.1 * sum(title_hits.values()))
    else:
        interest = 0.5  # no interests configured: neutral
    score = comment["score"]
    votes = _clip(math.log10(max(score or 0, 0) + 1) / 3) if score is not None else 0.0

    scores = {"applicability": applicability, "specificity": specificity, "evidence": evidence,
              "originality": originality, "interest": interest, "votes": votes}
    wsum = sum(cfg.weights.values()) or 1.0
    total = sum(cfg.weights[k] * v for k, v in scores.items()) / wsum
    if primary == "promo_spam":
        total *= 0.1
    too_short = len(body.strip()) < cfg.classifier.min_chars
    if too_short:
        total *= 0.5

    selected = total >= cfg.classifier.select_threshold and primary not in NOT_SELECTABLE and not too_short

    # ---- reasons (Russian, human-readable) -------------------------------
    reasons = [f"категория: {CATEGORY_LABELS[primary]}"]
    if steps:
        reasons.append(f"пошаговая структура: {steps} пункт(ов)")
    if unit_nums:
        reasons.append(f"конкретика: {unit_nums} значение(й) с единицами измерения")
    if reasons_n:
        reasons.append("есть обоснование (причинные связки)")
    if urls and not spam:
        reasons.append(f"ссылки на ресурсы: {len(urls)}")
    if source_words:
        reasons.append("упоминает источники/проверку")
    for name, v in sorted(own_hits.items(), key=lambda kv: -kv[1]):
        reasons.append(f"совпадает с интересом «{name}»")
    if max_sim >= 0.5 and similar_to:
        reasons.append(f"низкая оригинальность: похож на более ранний комментарий {similar_to}")
    if low_effort:
        reasons.append("короткая реплика без содержания")
    if spam:
        reasons.append("признаки рекламы/реферальных ссылок")
    if score is not None:
        reasons.append(f"голоса: {score} (только вспомогательный сигнал, не доказательство качества)")
    if not selected:
        if primary in NOT_SELECTABLE:
            reasons.append(f"не включён: категория «{CATEGORY_LABELS[primary]}» не попадает в подборки")
        elif too_short:
            reasons.append(f"не включён: короче {cfg.classifier.min_chars} символов")
        else:
            reasons.append(f"не включён: итоговая оценка {total:.2f} ниже порога {cfg.classifier.select_threshold:.2f}")
    top = sorted(((cfg.weights[k] * v, k) for k, v in scores.items()), reverse=True)[:2]
    if selected:
        reasons.insert(0, "включён: сильнее всего — " + ", ".join(CRITERIA_LABELS[k] for _, k in top))

    # ---- summary (extractive; original language of the key phrase is kept) --
    parts = [CATEGORY_LABELS[primary] + "."]
    details = []
    if steps:
        details.append(f"шагов: {steps}")
    if urls:
        domains = sorted({re.sub(r"^https?://(www\.)?", "", u).split("/")[0] for u in urls})
        details.append("ссылки: " + ", ".join(domains[:3]))
    if unit_nums:
        details.append(f"числовых параметров: {unit_nums}")
    if details:
        parts.append("Содержит " + "; ".join(details) + ".")
    key = _key_sentence(sentences)
    if key:
        parts.append(f"Ключевая фраза (оригинал): «{key}»")
    summary = " ".join(parts)

    topics = list(own_hits) or [n for n in title_hits]
    return Assessment(
        comment_id=comment["id"], post_id=comment["post_id"], primary_category=primary, categories=categories,
        scores={k: round(v, 3) for k, v in scores.items()}, total=round(total, 3), selected=selected,
        reasons=reasons + flags, summary=summary, topics=topics, flags=flags,
    )


def classify_all(store: Store, cfg: Config) -> dict:
    if cfg.classifier.backend != "rules":
        raise ValueError(
            f"classifier backend {cfg.classifier.backend!r} is not available. Only the local 'rules' backend is "
            "implemented; an external model needs a separate decision (see docs/SOURCES.md, section AI)."
        )
    fp = cfg.classifier_fingerprint()
    stats = {"assessed": 0, "selected": 0, "out_of_comment_period": 0}
    posts = store.posts_in_period(cfg.subreddit, cfg.period.start, cfg.period.end)
    for post in posts:
        comments = store.comments_for_post(post["id"])
        earlier: list[tuple[str, set]] = []
        per_thread: list[Assessment] = []
        with store.transaction():
            for c in comments:
                created = parse_iso(c["created_utc"])
                if cfg.comment_period and not cfg.comment_period.contains(created):
                    store.db.execute("DELETE FROM assessments WHERE comment_id=?", (c["id"],))
                    stats["out_of_comment_period"] += 1
                    continue
                a = assess_comment(cfg, c, post_title=post["title"], earlier_shingles=earlier)
                earlier.append((c["id"], _shingles(URL_RX.sub(" ", c["body"]))))
                per_thread.append(a)
            # Keep only the best N per thread selected.
            chosen = sorted((a for a in per_thread if a.selected), key=lambda a: -a.total)
            for a in chosen[cfg.classifier.max_per_thread:]:
                a.selected = False
                a.reasons.append(f"не включён: в теме уже {cfg.classifier.max_per_thread} лучших комментариев")
            for a in per_thread:
                store.db.execute(
                    """INSERT OR REPLACE INTO assessments(comment_id, post_id, primary_category, categories, scores,
                       total, selected, reasons, summary, topics, classifier, fingerprint, assessed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (a.comment_id, a.post_id, a.primary_category, json.dumps(a.categories, ensure_ascii=False),
                     json.dumps(a.scores), a.total, int(a.selected), json.dumps(a.reasons, ensure_ascii=False),
                     a.summary, json.dumps(a.topics, ensure_ascii=False), "rules", fp, now_iso()),
                )
                stats["assessed"] += 1
                stats["selected"] += int(a.selected)
    return stats
