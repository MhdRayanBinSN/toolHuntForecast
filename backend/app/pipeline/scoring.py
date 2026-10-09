import re


_NEGATIONS = {"no", "not", "never", "without", "doesnt", "dont", "isnt", "arent", "cant", "wont"}


def _tokens(value: str) -> list[str]:
    text = (value or "").casefold().replace("’", "'")
    for contraction in ("doesn't", "don't", "isn't", "aren't", "can't", "won't"):
        text = text.replace(contraction, contraction.replace("'", ""))
    return re.findall(r"[a-z0-9]+", text)


def _same_word(left: str, right: str) -> bool:
    if left == right:
        return True
    # Allow common singular/plural variation without stemming unrelated words.
    return (left.endswith("s") and left[:-1] == right) or (right.endswith("s") and right[:-1] == left)


def _phrase_evidence(text: str, phrase: str) -> dict | None:
    """Find a short, ordered phrase in one sentence and ignore negated mentions."""
    phrase_words = _tokens(phrase)
    if not phrase_words:
        return None
    for sentence in re.split(r"(?<=[.!?;:\n])\s*", text or ""):
        words = _tokens(sentence)
        for start, word in enumerate(words):
            if not _same_word(word, phrase_words[0]):
                continue
            matched = 1
            last = start
            max_position = min(len(words), start + len(phrase_words) + 4)
            for target in phrase_words[1:]:
                found = next((i for i in range(last + 1, max_position) if _same_word(words[i], target)), None)
                if found is None:
                    break
                matched += 1
                last = found
            if matched != len(phrase_words):
                continue
            if any(token in _NEGATIONS for token in words[max(0, start - 3):start]):
                continue
            quote = " ".join(sentence.split()).strip()
            return {"phrase": phrase, "quote": quote[:500]}
    return None


def category_verification_rules(category: str, profile=None, keywords=None) -> dict:
    """Resolve strict, explainable product-fit requirements for a category."""
    profile = profile if isinstance(profile, dict) else {}
    capabilities = profile.get("must_have_any")
    if isinstance(capabilities, list) and any(str(value).strip() for value in capabilities):
        required = [str(value).strip() for value in capabilities if str(value).strip()]
        source = "confirmed_category_profile"
    else:
        # Legacy categories predate profiles. Their configured phrases provide a
        # stricter fallback, but a category with no usable phrases fails closed.
        required = [str(value).strip() for value in (keywords or []) if len(_tokens(str(value))) >= 2]
        source = "configured_category_keywords"
    required = list(dict.fromkeys(required))
    return {
        "ready": len(required) >= 2,
        "required_phrases": required,
        "minimum_matches": 2,
        "profile_source": source,
        "adjacent_phrases": [str(value).strip() for value in profile.get("adjacent_not_accepted", []) if str(value).strip()],
        "negative_phrases": [str(value).strip() for value in profile.get("negative_keywords", []) if str(value).strip()],
        "category": category,
    }


def verify_category_fit(candidate: dict, rules: dict) -> dict:
    """Verify a candidate using its own homepage copy and the confirmed category profile."""
    page = candidate.get("homepage") if isinstance(candidate.get("homepage"), dict) else {}
    homepage_text = "\n".join(str(value or "") for value in (
        page.get("title", ""), page.get("description", ""),
        " ".join(page.get("headings", []) if isinstance(page.get("headings"), list) else []),
        page.get("text", ""),
    ))
    metadata_text = "\n".join(str(value or "") for value in (
        page.get("title", ""), page.get("description", ""), candidate.get("name", ""), candidate.get("tagline", ""),
    ))
    matched = []
    for phrase in rules.get("required_phrases", []):
        evidence = _phrase_evidence(homepage_text, phrase)
        if evidence:
            matched.append(evidence)
    adjacent = [evidence for phrase in rules.get("adjacent_phrases", [])
                if (evidence := _phrase_evidence(metadata_text, phrase))]
    negative = [evidence for phrase in rules.get("negative_phrases", [])
                if (evidence := _phrase_evidence(metadata_text, phrase))]
    required_count = len(rules.get("required_phrases", []))
    minimum = int(rules.get("minimum_matches", 2))
    fit = len(matched) / required_count if required_count else 0.0
    passed = bool(rules.get("ready")) and len(matched) >= minimum and not adjacent and not negative
    reasons = []
    if len(matched) < minimum:
        reasons.append(f"Homepage supports {len(matched)} of {required_count} category capabilities; at least {minimum} are required.")
    if adjacent:
        reasons.append("Homepage identifies the product with an adjacent category: " + ", ".join(item["phrase"] for item in adjacent) + ".")
    if negative:
        reasons.append("Homepage contains an excluded product signal: " + ", ".join(item["phrase"] for item in negative) + ".")
    if not rules.get("ready"):
        reasons.append("This category has no usable verification profile or keyword phrases. Update and confirm the category before running research.")
    return {
        "accepted": passed,
        "category_fit": round(fit, 4),
        "profile_source": rules.get("profile_source", "unknown"),
        "matched_capabilities": matched,
        "missing_capabilities": [phrase for phrase in rules.get("required_phrases", []) if phrase not in {item["phrase"] for item in matched}],
        "adjacent_matches": adjacent,
        "negative_matches": negative,
        "reasons": reasons,
    }


def candidate_score(candidate: dict, category: str, index: int = 0, liveness: float = 0.5,
                    keywords=None, category_fit: float | None = None) -> dict:
    if category_fit is None:
        # Kept for category evidence probes and legacy display. Pipeline selection
        # always supplies the homepage verification score explicitly.
        text = (candidate.get("name", "") + " " + candidate.get("tagline", "") + " " + candidate.get("description", "")).lower()
        phrases = [category, *(keywords or [])]
        text_words = set(_tokens(text))
        fits = []
        for phrase in phrases:
            words = [word for word in _tokens(str(phrase)) if len(word) > 2]
            if words:
                fits.append(sum(word in text_words for word in words) / len(words))
        category_fit = max(fits, default=0.0)
    novelty = 0.5
    velocity = min(1.0, float(candidate.get("votes", 0)) / 100)
    source_count = 1 / 3 if candidate.get("source") else 0
    live = 1.0 if liveness else 0.0
    total = 0.30 * category_fit + 0.25 * novelty + 0.15 * velocity + 0.15 * source_count + 0.15 * live
    return {"category_fit": category_fit, "novelty": novelty, "velocity": velocity,
            "source_count": source_count, "liveness": live, "total": total}
