from datetime import date
from unittest.mock import patch

from digest import Article, canonical_key, fallback_digest, period, render


def test_period_uses_previous_seven_complete_days() -> None:
    assert period(date(2026, 8, 31)) == (date(2026, 8, 24), date(2026, 8, 30))


def test_canonical_key_normalizes_punctuation_and_case() -> None:
    assert canonical_key("Hello, WORLD!") == "hello world"


def test_fallback_and_render_keep_source_link() -> None:
    article = Article("world-1", "world", "Title", "https://example.com/a", "example.com", "20260830", "Summary")
    with patch("digest.translate_ru", side_effect=lambda text, session: text):
        digest = fallback_digest([article])
    html_body, text_body = render(digest, [article], date(2026, 8, 24), date(2026, 8, 30))
    assert "https://example.com/a" in html_body
    assert "https://example.com/a" in text_body
