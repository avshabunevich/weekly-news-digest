"""Deterministic newsworthiness ranking, independent of RSS ordering or AI access."""
import re


WORLD_QUERIES = (
    '(world OR international) (source:Reuters OR source:AP OR source:BBC)',
    '(war OR ceasefire OR election OR diplomacy OR sanctions)',
    '(terrorist OR terrorism OR hijacking OR "terror plot" OR "suicide attack" OR "cockpit attack" OR "plane crash")',
    '(terrorist OR hijacking OR "suicide attack") (source:Reuters OR source:AP OR source:AFP)',
    '(earthquake OR tsunami OR hurricane OR pandemic OR "mass casualties" OR assassination OR coup)',
)
STOP_WORDS = set('a an the of to in on at for from with and or as by is are was were has have had '
                 'says said say new news latest live updates update after over amid about into '
                 'it its this that will could would may co pilot'.split())
TRUSTED_SOURCES = {
    'reuters': 'reuters', 'reuters.com': 'reuters',
    'ap': 'ap', 'ap news': 'ap', 'associated press': 'ap', 'the associated press': 'ap', 'apnews.com': 'ap',
    'afp': 'afp', 'afp.com': 'afp', 'bbc': 'bbc', 'bbc news': 'bbc', 'bbc.com': 'bbc', 'bbc.co.uk': 'bbc',
    'the guardian': 'guardian', 'theguardian.com': 'guardian',
    'al jazeera': 'aljazeera', 'aljazeera.com': 'aljazeera',
    'financial times': 'ft', 'ft.com': 'ft',
}
IMPACT = (
    r'\b(terrorist|terrorism|hijack\w*|terror plot|suicide attack|mass shooting|assassinat\w*)\b',
    r'\b(earthquake|tsunami|hurricane|famine|pandemic|plane crash|mass casualties)\b',
    r'\b(invasion|ceasefire|declares war|nuclear|coup|election|sanctions|peace deal|default|financial crisis)\b',
)


def source_key(source):
    key = source.casefold().removeprefix('www.').strip()
    return TRUSTED_SOURCES.get(key, key)


def trusted(article):
    return article.source.casefold().removeprefix('www.').strip() in TRUSTED_SOURCES


def tokens(title):
    return set(re.findall(r'[a-z0-9а-яё]+', title.casefold())) - STOP_WORDS


def same_event(left, right):
    # Named storms have different headlines as they develop.
    storm_pattern = r'\b(?:[Hh]urricane|[Tt]yphoon|[Cc]yclone|[Ss]torm)\s+([A-Z][a-z]+)\b'
    storms_a = set(re.findall(storm_pattern, left.title))
    storms_b = set(re.findall(storm_pattern, right.title))
    if any(name.casefold() in tokens(right.title) for name in storms_a) or any(
        name.casefold() in tokens(left.title) for name in storms_b
    ):
        return True
    a, b = tokens(left.title), tokens(right.title)
    common = a & b
    return bool(a and b) and (a == b or (len(common) >= 3 and len(common) / min(len(a), len(b)) >= .55))


def rank_articles(articles):
    """One representative per event; broad coverage outranks recency alone.

    Publisher count is a prominence proxy, not independent fact verification.
    Impact keywords only boost stories covered by a recognized source or at
    least two publishers. No company or particular incident is hardcoded.
    """
    clusters = []
    for article in sorted(articles, key=lambda a: (not trusted(a), a.title, a.url)):
        group = next((g for g in clusters if same_event(article, g[0])), None)
        if group is None:
            clusters.append([article])
        else:
            group.append(article)
    ranked = []
    for group in clusters:
        representative = max(group, key=lambda a: (trusted(a), a.published, len(a.description)))
        sources = {source_key(a.source) for a in group}
        reliable = any(trusted(a) for a in group)
        impact = max((sum(bool(re.search(pattern, a.title, re.I)) for pattern in IMPACT) for a in group), default=0)
        score = min(len(sources), 5) * 3 + int(reliable) * 4
        if reliable or len(sources) >= 2:
            score += min(impact, 2) * 5
        ranked.append((score, representative))
    ranked.sort(key=lambda pair: (-pair[0], -int(pair[1].published or '0'), pair[1].title))
    return [article for _, article in ranked]


def select_world(articles, count=3):
    ranked = rank_articles(articles)
    chosen = ranked[:count]
    # Up to two overflow items keep serious, attributable incidents visible
    # during crowded weeks. This is an editorial heuristic, not fact checking.
    for pattern in IMPACT[:2]:
        if any(re.search(pattern, a.title, re.I) for a in chosen):
            continue
        extra = next((a for a in ranked[count:] if trusted(a) and re.search(pattern, a.title, re.I)), None)
        if extra is not None:
            chosen.append(extra)
    return chosen
