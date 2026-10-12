"""Deterministic matching of discovered pages to configured research topics."""

import re
from collections.abc import Iterable, Mapping


_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {"a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "to", "what", "with"}
DEFAULT_MIN_SCORE = 0.55


def _values(value) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,;\n]+", value)
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = [str(item) for item in value]
    else:
        values = []
    return list(dict.fromkeys(" ".join(item.split()).strip() for item in values if item and item.strip()))


def category_config_value(name: str, config: Mapping | None):
    """Look up category configuration by normalized exact name, including plurals."""
    def normalize(value):
        words = []
        for word in " ".join(str(value).casefold().split()).split():
            if word.endswith("ies") and len(word) > 4:
                word = word[:-3] + "y"
            elif word.endswith("s") and not word.endswith(("ss", "us")) and len(word) > 4:
                word = word[:-1]
            words.append(word)
        return tuple(words)

    target = normalize(name)
    if not isinstance(config, Mapping):
        return []
    for key, value in config.items():
        if normalize(key) == target:
            return value
    return []


def topic_definitions(config: Mapping | None) -> dict[str, list[str]]:
    raw = config.get("page_topics", {}) if isinstance(config, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {}
    definitions = {}
    for name, description in raw.items():
        key = str(name).strip().casefold()
        phrases = _values(description)
        if key:
            definitions[key] = list(dict.fromkeys([key.replace("_", " "), *phrases]))
    return definitions


def required_topics(config: Mapping | None, definitions: Mapping[str, list[str]]) -> list[str]:
    raw = config.get("required_topics", []) if isinstance(config, Mapping) else []
    return list(dict.fromkeys(name.casefold() for name in _values(raw)))


def _tokens(text: str) -> list[str]:
    tokens=[]
    for token in _WORD.findall((text or "").casefold()):
        if token.endswith("ies") and len(token)>4:
            token=token[:-3]+"y"
        elif token.endswith("s") and not token.endswith(("ss","us")) and len(token)>4:
            token=token[:-1]
        if token not in _STOP_WORDS:
            tokens.append(token)
    return tokens


def _phrase_score(text: str, phrases: list[str]) -> tuple[float, list[str]]:
    sentences = [_tokens(part) for part in re.split(r"(?<=[.!?;:\n])\s*", text or "")]
    sentences = [sentence for sentence in sentences if sentence]
    if not sentences:
        return 0.0, []
    best_score = 0.0
    matched = []
    for phrase in phrases:
        phrase_tokens = _tokens(phrase)
        if not phrase_tokens:
            continue
        score = 0.0
        for tokens in sentences:
            if len(phrase_tokens) == 1:
                sentence_score = 1.0 if phrase_tokens[0] in tokens else 0.0
            else:
                sentence_score = 0.0
                width = len(phrase_tokens) + 2
                for start in range(len(tokens)):
                    window = tokens[start:start + width]
                    matched_count = sum(token in window for token in phrase_tokens)
                    sentence_score = max(sentence_score, matched_count / len(phrase_tokens))
                    if sentence_score == 1.0:
                        break
            score = max(score, sentence_score)
        if score >= 0.75:
            matched.append(phrase)
        best_score = max(best_score, score)
    return round(best_score, 4), list(dict.fromkeys(matched))


def match_link_topics(url: str, anchor_text: str, definitions: Mapping[str, list[str]], min_score: float = DEFAULT_MIN_SCORE) -> list[dict]:
    matches = []
    for name, phrases in definitions.items():
        url_score, url_phrases = _phrase_score(url, [name.replace("_", " "), *phrases])
        anchor_score, anchor_phrases = _phrase_score(anchor_text, [name.replace("_", " "), *phrases])
        score = max(url_score * 0.8, anchor_score)
        if score >= min_score:
            matches.append({"topic": name, "score": round(score, 4), "matched_phrases": list(dict.fromkeys(url_phrases + anchor_phrases))})
    return sorted(matches, key=lambda item: (-item["score"], item["topic"]))


def classify_page_topics(page: Mapping, definitions: Mapping[str, list[str]], min_score: float = DEFAULT_MIN_SCORE) -> list[dict]:
    title = str(page.get("title") or "")
    description = str(page.get("description") or "")
    headings = page.get("headings") if isinstance(page.get("headings"), list) else []
    body = str(page.get("text") or "")
    metadata = " ".join([title, description, *map(str, headings)])
    matches = []
    for name, phrases in definitions.items():
        aliases = [name.replace("_", " "), *phrases]
        metadata_score, metadata_phrases = _phrase_score(metadata, aliases)
        body_aliases = [phrase for phrase in aliases if len(_tokens(phrase)) > 1]
        body_score, body_phrases = _phrase_score(body, body_aliases)
        if body_phrases and all(len(_tokens(phrase)) == 1 for phrase in body_phrases):
            body_score=min(body_score,0.5)
        score = max(metadata_score, body_score * 0.75)
        if score >= min_score:
            matches.append({
                "topic": name,
                "score": round(score, 4),
                "matched_phrases": list(dict.fromkeys(metadata_phrases + body_phrases)),
            })
    return sorted(matches, key=lambda item: (-item["score"], item["topic"]))


def select_topic_links(links: list[dict], definitions: Mapping[str, list[str]], required: list[str], limit: int, min_score: float = DEFAULT_MIN_SCORE) -> list[dict]:
    scored = []
    for link in links:
        matches = match_link_topics(link.get("url", ""), link.get("anchor", ""), definitions, min_score)
        if matches:
            scored.append({**link, "topic_matches": matches, "score": matches[0]["score"]})

    chosen = []
    selected_urls = set()
    for topic in required:
        candidates = [item for item in scored if item["url"] not in selected_urls and any(match["topic"] == topic for match in item["topic_matches"])]
        if candidates:
            best = max(candidates, key=lambda item: (next(match["score"] for match in item["topic_matches"] if match["topic"] == topic), item["url"]))
            match = next(match for match in best["topic_matches"] if match["topic"] == topic)
            chosen.append({**best, "topic": topic, "score": match["score"]})
            selected_urls.add(best["url"])

    for item in sorted(scored, key=lambda value: (-value["score"], value["url"])):
        if item["url"] in selected_urls:
            continue
        chosen.append({**item, "topic": item["topic_matches"][0]["topic"]})
        selected_urls.add(item["url"])
        if len(chosen) >= max(0, limit):
            break
    return chosen[:max(0, limit)]


def topic_coverage(pages: list[dict], required: list[str]) -> dict:
    observed = set()
    for page in pages:
        if page.get("topic"):
            observed.add(page["topic"])
        observed.update(page.get("matched_topics", []))
    covered = [topic for topic in required if topic in observed]
    return {"required": required, "covered": covered, "missing": [topic for topic in required if topic not in covered]}


def allowed_fact_fields(topic: str, config: Mapping | None) -> set[str] | None:
    raw = config.get("topic_fact_fields", {}) if isinstance(config, Mapping) else {}
    fields = raw.get(topic) if isinstance(raw, Mapping) else None
    return set(_values(fields)) if fields else None
