from datetime import date
from unittest.mock import Mock, patch

from digest import Article, collect_articles, fallback_digest, google_news_records, select_articles
from news_selection import rank_articles


def article(number, title, source='Reuters', published='20261009'):
    return Article(str(number), 'world', title, f'https://example.org/{number}', source, published)


def test_major_aviation_story_survives_rss_position_and_duplicates():
    routine = [article(i, f'Minister visits country {i} for routine talks') for i in range(20)]
    incident = article(30, 'Flydubai co-pilot plotted suicide attack on Israel, UAE says', 'Al Jazeera')
    followup = article(31, 'Flydubai co-pilot planned suicide attack on Tel Aviv airport: UAE prosecutor', 'AFP')
    candidates = routine + [incident, followup]
    selected = select_articles(candidates)
    assert selected[0] in (incident, followup)
    assert sum('Flydubai' in a.title for a in selected) == 1
    with patch('digest.translate_ru', side_effect=lambda text, session: text):
        digest = fallback_digest(candidates)
    assert digest['sections'][0]['items'][0]['id'] in ('30', '31')


def test_coverage_matters_without_danger_keywords():
    story = 'Landmark global treaty approved by national leaders'
    candidates = [article(1, 'Minister visits parliament'), article(2, story), article(3, story, 'BBC News')]
    assert rank_articles(candidates)[0].title == story
    assert len(rank_articles(candidates)) == 2


def test_repeated_publisher_does_not_inflate_coverage():
    single = [article(i, 'Minister visits parliament', 'Reuters' if i % 2 else 'reuters.com') for i in range(20)]
    major = article(30, 'Earthquake destroys towns in coastal region', 'AP News')
    assert rank_articles(single + [major])[0] == major


def test_named_storm_updates_are_one_event():
    candidates = [article(1, 'Isaias makes landfall as hurricane, hammering US Gulf Coast'),
                  article(2, 'Hurricane Isaias: Four dead and thousands without power', 'Al Jazeera'),
                  article(3, 'Hurricane Simon threatens Mexico', 'BBC')]
    assert len(rank_articles(candidates)) == 2


def test_crowded_week_keeps_important_incident_in_overflow():
    candidates = []
    for number, title in enumerate(('National leaders sign global trade agreement',
                                    'Parliament approves historic constitutional reform',
                                    'Scientists announce major medical discovery')):
        candidates.extend(article(f'{number}-{source}', title, source)
                          for source in ('Reuters', 'AP News', 'BBC', 'AFP', 'The Guardian'))
    incident = article(30, 'Airline crew foils terrorist attack on passenger flight')
    selected = select_articles(candidates + [incident])
    assert len(selected) == 4
    assert selected[-1] == incident


def test_wire_coverage_of_real_flydubai_headlines_is_combined():
    candidates = [article(1, 'Flydubai co-pilot planned suicide attack on Tel Aviv airport: UAE prosecutor', 'AFP.com'),
                  article(2, 'Flydubai co-pilot plotted suicide attack on Israel, UAE says', 'Al Jazeera'),
                  article(3, 'UAE Says Omani Flydubai Co-Pilot Planned Suicide Attack On Tel Aviv Flight', 'Deccan Chronicle')]
    assert len(rank_articles(candidates)) == 1


def test_distinct_disasters_are_not_merged_and_rumor_not_promoted():
    japan = article(1, 'Earthquake strikes Japan and destroys coastal towns')
    chile = article(2, 'Earthquake strikes Chile damaging hospitals and roads')
    rumor = article(3, 'Rumored terrorist nuclear suicide attack', 'unknown blog')
    result = rank_articles([rumor, japan, chile])
    assert len(result) == 3
    assert result[-1] == rumor


def test_collection_includes_security_query_and_keeps_late_candidates():
    def records(session, query, start, end):
        if 'terrorism' in query:
            return [{'title': 'Airline hijacking thwarted by crew', 'url': 'https://example.org/attack',
                     'domain': 'Reuters', 'seendate': '20261009'}]
        return [{'title': f'World government talks number {i}', 'url': f'https://example.org/{i}',
                 'domain': 'Reuters', 'seendate': '20261010'} for i in range(20)]
    with patch('digest.google_news_records', side_effect=records):
        result = collect_articles(date(2026, 10, 5), date(2026, 10, 11))
    assert len([a for a in result if a.section == 'world']) == 21
    assert any('hijacking' in a.title for a in select_articles(result))


def test_historical_search_uses_dates_and_rejects_missing_or_outside_date():
    session = Mock()
    session.get.return_value.content = b'''<rss><channel>
      <item><title>Valid</title><link>https://a/1</link><pubDate>Fri, 09 Oct 2026 12:00:00 GMT</pubDate></item>
      <item><title>Old</title><link>https://a/2</link><pubDate>Fri, 02 Oct 2026 12:00:00 GMT</pubDate></item>
      <item><title>Unknown date</title><link>https://a/3</link></item>
    </channel></rss>'''
    records = google_news_records(session, 'news when:7d', date(2026, 10, 5), date(2026, 10, 11))
    assert [r['title'] for r in records] == ['Valid']
    query = session.get.call_args.kwargs['params']['q']
    assert 'when:' not in query
    assert 'after:2026-10-04 before:2026-10-12' in query
