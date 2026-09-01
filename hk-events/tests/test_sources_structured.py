"""Parser tests for the two structured-data sources, driven from saved fixtures.

NO NETWORK. Every test here reads `tests/fixtures/*.html`, captured live on
2026-09-01 and trimmed to the `<script>` island each parser actually reads. The
script contents are byte-for-byte as served — deliberately NOT tidied up, because
a fixture cleaned until it parses proves nothing about the real page.

Two things are under test, and the second matters more than the first:

  1. the parsers pull the right events out of real markup, and
  2. the RAISE-VS-EMPTY contract holds. An adapter that returns `[]` when it
     could not read the page is scored a SUCCESS by `source_health`, which zeroes
     a live failure streak and stamps a `last_success` that never happened. So
     "missing script tag" and "unparseable JSON" must raise, and only a page that
     genuinely listed nothing may return `[]`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hk_events.dedupe import collapse_cross_source
from hk_events.errors import SourceFetchError, SourceNotConfiguredError
from hk_events.schema import Event
from hk_events.sources import aitinkerers, luma, luma_discover
from hk_events.sources._html_common import load_page_url

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# 2a — AI Tinkerers, schema.org JSON-LD
# ---------------------------------------------------------------------------

def test_aitinkerers_parses_every_distinct_event_from_the_real_page():
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    # 11 Event entries live across two overlapping ItemLists; 7 are distinct.
    assert len(events) == 7
    assert all(e.source == "aitinkerers" for e in events)
    assert len({e.external_id for e in events}) == 7


def test_aitinkerers_maps_an_event_completely():
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    hackathon = next(e for e in events if "Global Hackathon" in e.title)

    assert hackathon.title == "Agents, Everywhere: Bots, Channels, & More — Global Hackathon"
    assert hackathon.start == datetime(
        2026, 9, 12, 10, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert hackathon.end == datetime(
        2026, 9, 12, 17, 0, tzinfo=timezone(timedelta(hours=8))
    )
    # url is the clickable page, NOT the '#event' @id fragment.
    assert hackathon.url == (
        "https://hong-kong.aitinkerers.org/p/"
        "agents-everywhere-bots-channels-more-global-hackathon"
    )
    assert "#" not in hackathon.url
    # Nested Place + PostalAddress flattened to plain text — never a URL, because
    # render() would turn a link-shaped location into a dead [register] link.
    assert hackathon.location is not None
    assert "100 Cyberport Road" in hackathon.location
    assert not hackathon.location.startswith("http")
    assert hackathon.organizer == "AI Tinkerers - Hong Kong"
    assert hackathon.description


def test_aitinkerers_ignores_non_event_types_sharing_the_same_itemlists():
    """The ItemLists also carry BlogPosting/TechArticle/CreativeWork members, and
    the page has Organization/WebSite/CollectionPage/BreadcrumbList blocks. A
    talk archive and a testimonial wall are not events."""
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    titles = {e.title for e in events}
    # A TechArticle headline from the talks ItemList.
    assert not any("Learn Anything by Doing" in t for t in titles)
    # Every survivor came from an object we could date or at least name.
    assert all(e.title for e in events)


def test_aitinkerers_parser_does_not_filter_past_events():
    """Horizon filtering belongs to `_within_horizon`, not the parser — there must
    be exactly one definition of 'too old' in this codebase."""
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    starts = sorted(e.start for e in events if e.start)
    # The fixture's oldest event is May 2026 — long outside any 45-day horizon.
    assert starts[0].date().isoformat() == "2026-05-07"
    assert sum(1 for s in starts if s.year == 2026 and s.month < 9) == 5


def test_aitinkerers_horizon_filter_is_what_drops_the_past():
    """Same fixture, run through the real horizon window: only the future survives."""
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    from hk_events.sources._html_common import _within_horizon

    # Anchor on the capture date so this does not rot: with a 45-day horizon from
    # 2026-09-01, the two upcoming events (12 Sep, 29 Sep) are in and the five
    # past ones are out. Assert the property, not today's clock.
    capture = datetime(2026, 9, 1, tzinfo=timezone.utc)
    upcoming = [e for e in events if e.start and e.start > capture]
    assert len(upcoming) == 2
    assert {e.start.date().isoformat() for e in upcoming} == {"2026-09-12", "2026-09-29"}
    # And the live filter is a real function that rejects the old ones.
    ancient = next(e for e in events if e.start and e.start.date().isoformat() == "2026-05-07")
    assert _within_horizon(ancient.start) is False


def test_aitinkerers_raises_when_the_json_ld_island_is_gone():
    with pytest.raises(SourceFetchError) as exc:
        aitinkerers._parse_jsonld_events("<html><body><h1>Hong Kong</h1></body></html>")
    assert "ld+json" in str(exc.value)


def test_aitinkerers_raises_when_every_block_is_unparseable():
    html = '<html><body><script type="application/ld+json">{not json</script></body></html>'
    with pytest.raises(SourceFetchError):
        aitinkerers._parse_jsonld_events(html)


def test_aitinkerers_returns_empty_for_a_page_with_no_events():
    """Parsed fine, listed nothing. That is a real observation, not a failure —
    returning [] here is what lets `source_health` record an honest success."""
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@type": "ItemList", "itemListElement": ['
        '{"@type":"ListItem","item":{"@type":"BlogPosting","name":"Recap"}}]}'
        "</script></body></html>"
    )
    assert aitinkerers._parse_jsonld_events(html) == []


def test_aitinkerers_accepts_a_flattened_itemlist():
    """Live markup wraps each Event in a ListItem. Accept a bare Event too, so a
    future flattening of the page does not read as 'zero events this week'."""
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@type": "ItemList", "itemListElement": ['
        '{"@type":"Event","@id":"https://x/p/a#event","name":"Bare Event",'
        '"startDate":"2026-09-20T18:00:00+08:00"}]}'
        "</script></body></html>"
    )
    events = aitinkerers._parse_jsonld_events(html)
    assert [e.title for e in events] == ["Bare Event"]
    assert events[0].url == "https://x/p/a"


def test_aitinkerers_is_still_auto_tagged_founder_ai():
    """The classifier short-circuits this source without an LLM call. The rewrite
    kept the source NAME, so that must still hold."""
    from hk_events.classifier import AUTO_FOUNDER_SOURCES, classify

    assert "aitinkerers" in AUTO_FOUNDER_SOURCES
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    result = classify(events[0])
    assert result.tag == "founder_ai"
    assert result.surface


@pytest.mark.parametrize("group", ["aitinkerers", "luma_discover"])
@pytest.mark.parametrize(
    "cfg, why",
    [
        ({}, "the whole scrape_pages section is missing"),
        ({"scrape_pages": {}}, "the section is present but empty"),
        ({"scrape_pages": {"aitinkerers": {"url": ""}, "luma_discover": {"url": ""}}}, "the url is blank"),
        (
            {"scrape_pages": {"aitinkerers": {"url": "TODO-confirm"}, "luma_discover": {"url": "TODO-confirm"}}},
            "the entry is parked with TODO",
        ),
    ],
    ids=["absent", "empty", "blank", "todo"],
)
def test_unconfigured_page_is_neither_success_nor_failure(monkeypatch, group, cfg, why):
    """`SourceNotConfiguredError`, NOT `[]` and NOT `SourceFetchError`.

    Nobody asked us anything, so the run is no evidence about this source. The
    orchestrator puts it in neither `succeeded` nor `errors` and `update_health`
    prunes the record — where returning `[]` would be scored a SUCCESS, zeroing a
    real failure streak and stamping a `last_success` that never happened.
    """
    monkeypatch.setattr("hk_events.sources._html_common._load_sources_yaml", lambda: cfg)
    with pytest.raises(SourceNotConfiguredError):
        load_page_url(group, source=group)


# ---------------------------------------------------------------------------
# 2b — Luma discovery, Next.js __NEXT_DATA__
# ---------------------------------------------------------------------------

def test_luma_discover_parses_the_real_city_page():
    events = luma_discover._parse_next_data(_fixture("luma_hong_kong.html"))
    assert len(events) == 12
    assert all(e.source == "luma_discover" for e in events)
    assert len({e.external_id for e in events}) == 12
    assert all(e.external_id.startswith("evt-") for e in events)


def test_luma_discover_maps_an_event_completely():
    events = luma_discover._parse_next_data(_fixture("luma_hong_kong.html"))
    ev = next(e for e in events if e.external_id == "evt-XfjhlRUHw3Bhz0S")

    assert ev.title == "Why Your AI App Looks Bad (and How to Fix It)"
    assert ev.start == datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
    assert ev.end == datetime(2026, 9, 2, 13, 30, tzinfo=timezone.utc)
    # URL is built from the slug, not the api_id, so it matches what a human
    # would be handed by the organiser.
    assert ev.url == "https://lu.ma/f3xg5xlu"
    assert ev.location == "Soho House Hong Kong, 33 Des Voeux Rd W, Sheung Wan, Hong Kong"
    assert ev.raw["canonical_id"] == "luma-evt:evt-XfjhlRUHw3Bhz0S"


def test_luma_discover_parser_does_not_filter_by_date():
    """Same rule as AI Tinkerers: the horizon window is the shared one, applied by
    the fetch path, not baked into the parser."""
    events = luma_discover._parse_next_data(_fixture("luma_hong_kong.html"))
    # Nothing was dropped — all 12 objects the walk found became Events.
    data = json.loads(
        _fixture("luma_hong_kong.html").split('type="application/json">', 1)[1].rsplit("</script>", 1)[0]
    )
    raw_events = data["props"]["pageProps"]["initialData"]["data"]["events"]
    assert len(events) == len(raw_events)


def test_luma_discover_raises_when_next_data_is_missing():
    """A challenge page or a framework change is 'could not look', not 'quiet week'."""
    with pytest.raises(SourceFetchError) as exc:
        luma_discover._parse_next_data("<html><body>Just a moment...</body></html>")
    assert "__NEXT_DATA__" in str(exc.value)


def test_luma_discover_raises_on_unparseable_next_data():
    html = '<html><body><script id="__NEXT_DATA__">{"props": </script></body></html>'
    with pytest.raises(SourceFetchError):
        luma_discover._parse_next_data(html)


def test_luma_discover_returns_empty_on_a_genuinely_quiet_page():
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"initialData": {"data": {"events": []}}}}}'
        "</script></body></html>"
    )
    assert luma_discover._parse_next_data(html) == []


def test_luma_discover_finds_events_at_an_unexpected_path():
    """The walk is structural, not path-based, because __NEXT_DATA__ is an
    internal build artefact Luma can reshape without notice."""
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"somethingNew":{"rows":[{"item":{"api_id":"evt-ZZZ",'
        '"name":"Moved Event","start_at":"2026-09-20T10:00:00.000Z","url":"abc123"}}]}}}'
        "</script></body></html>"
    )
    events = luma_discover._parse_next_data(html)
    assert [(e.external_id, e.url) for e in events] == [("evt-ZZZ", "https://lu.ma/abc123")]


def test_luma_discover_skips_an_event_with_no_api_id():
    """Without an api_id there is no stable id and no way to collide with the
    .ics adapter — it would be re-notified every run AND double-reported."""
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"events":[{"name":"Anonymous","start_at":"2026-09-20T10:00:00.000Z"}]}}'
        "</script></body></html>"
    )
    assert luma_discover._parse_next_data(html) == []


def test_the_shipped_config_actually_configures_both_pages():
    """Guards the other half: the adapters are only wired if config/sources.yaml
    really carries these URLs. A rename of the YAML key would otherwise turn both
    new sources into a silent, permanently-pruned no-op."""
    assert load_page_url("aitinkerers", source="aitinkerers") == "https://hong-kong.aitinkerers.org/"
    # lu.ma/hk is a stale 2023 event, NOT the city page. This assertion is the
    # tripwire for someone "shortening" the URL.
    assert load_page_url("luma_discover", source="luma_discover") == "https://lu.ma/hong-kong"


# ---------------------------------------------------------------------------
# The overlap: `luma` (.ics) and `luma_discover` seeing one event
# ---------------------------------------------------------------------------

def test_luma_ics_uid_yields_the_same_canonical_id_as_the_city_page():
    """The two adapters derive the collision key independently, from different
    raw material: the .ics UID `evt-<api_id>@events.lu.ma`, and the city page's
    bare `api_id`. If these ever stop agreeing, duplicates come back."""
    assert luma._uid_canonical_id("evt-cuDFACZOa8zGKRu@events.lu.ma") == "luma-evt:evt-cuDFACZOa8zGKRu"
    assert luma_discover.canonical_id("evt-cuDFACZOa8zGKRu") == "luma-evt:evt-cuDFACZOa8zGKRu"


def test_luma_calendar_row_uid_has_no_canonical_id():
    """`calev-` UIDs are calendar ROWS for events not hosted on Luma. They own no
    api_id, and no city-page event can ever be one — so falling back to the
    source-prefixed dedup_key is correct, not a missed collision."""
    assert luma._uid_canonical_id("calev-1huzvBb6z8QJmNv@events.lu.ma") is None


def _luma_pair() -> tuple[Event, Event]:
    """The real overlap, as observed on 2026-09-01: `Paperclip-maxxing Capitalism`
    was on the startupshk .ics feed AND on lu.ma/hong-kong at the same time."""
    api_id = "evt-cuDFACZOa8zGKRu"
    start = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    from_ics = Event(
        source="luma",
        external_id=f"{api_id}@events.lu.ma",
        title="Paperclip-maxxing Capitalism",
        url="https://luma.com/10d30z36",
        start=start,
        description="from the .ics feed",
        raw={"uid": f"{api_id}@events.lu.ma", "canonical_id": f"luma-evt:{api_id}"},
    )
    from_page = Event(
        source="luma_discover",
        external_id=api_id,
        title="Paperclip-maxxing Capitalism",
        url="https://lu.ma/10d30z36",
        start=start,
        raw={"api_id": api_id, "canonical_id": f"luma-evt:{api_id}"},
    )
    return from_ics, from_page


def test_the_two_luma_sources_share_an_identity_key():
    from_ics, from_page = _luma_pair()
    assert from_ics.identity_key == from_page.identity_key == "luma-evt:evt-cuDFACZOa8zGKRu"
    # ...and dedup_key, the pre-existing key, does NOT collapse them. This is the
    # assertion that documents why collapse_cross_source had to be written.
    assert from_ics.dedup_key != from_page.dedup_key


def test_collapse_merges_the_overlapping_pair_into_one():
    from_ics, from_page = _luma_pair()
    kept, collapsed = collapse_cross_source([from_ics, from_page], seen_lookup=lambda s: {})
    assert len(kept) == 1
    assert len(collapsed) == 1
    # Neither has been seen before, so the fixed precedence decides: `luma` wins
    # because the .ics carries a description the listing card does not.
    assert kept[0].source == "luma"
    assert kept[0].description == "from the .ics feed"


def test_collapse_keeps_the_source_that_already_saw_it():
    """Continuity beats precedence. An event first found by `luma_discover` lives
    in seen_luma_discover.json; handing the win to `luma` on a later run would
    look it up in the wrong seen-set, miss, and notify it a SECOND time — and
    write a second calendar entry, since stable_hash mixes the source in."""
    from_ics, from_page = _luma_pair()
    seen = {"luma_discover": {from_page.external_id: {"stages": ["new"], "tag": "founder_ai"}}}
    kept, collapsed = collapse_cross_source(
        [from_ics, from_page], seen_lookup=lambda s: seen.get(s, {})
    )
    assert [e.source for e in kept] == ["luma_discover"]
    assert collapsed[0][1].source == "luma"


def test_collapse_is_order_independent():
    from_ics, from_page = _luma_pair()
    for pair in ([from_ics, from_page], [from_page, from_ics]):
        kept, _ = collapse_cross_source(pair, seen_lookup=lambda s: {})
        assert [e.source for e in kept] == ["luma"]


def test_collapse_leaves_unrelated_events_alone():
    from_ics, from_page = _luma_pair()
    other = Event(
        source="meetup",
        external_id="meetup-1",
        title="Something else",
        url="https://meetup.com/x",
        start=from_ics.start,
    )
    calendar_row = Event(
        source="luma",
        external_id="calev-1huzvBb6z8QJmNv@events.lu.ma",
        title="Hong Kong FinTech Week",
        url="https://www.fintechweek.hk/",
        start=from_ics.start,
        raw={"uid": "calev-1huzvBb6z8QJmNv@events.lu.ma"},
    )
    kept, collapsed = collapse_cross_source(
        [from_ics, other, calendar_row, from_page], seen_lookup=lambda s: {}
    )
    assert len(kept) == 3
    assert len(collapsed) == 1
    assert {e.external_id for e in kept} >= {"meetup-1", "calev-1huzvBb6z8QJmNv@events.lu.ma"}


def test_two_same_titled_events_from_different_sources_are_not_collapsed():
    """The collapse is identity-based, not fuzzy. Two genuinely different events
    that happen to share a title must both survive — a false merge silently
    deletes an event from the digest, which is worse than a duplicate."""
    a = Event(source="meetup", external_id="a", title="AI Meetup", url="https://a", start=None)
    b = Event(source="luma", external_id="b", title="AI Meetup", url="https://b", start=None)
    kept, collapsed = collapse_cross_source([a, b], seen_lookup=lambda s: {})
    assert len(kept) == 2
    assert collapsed == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_both_new_sources_are_wired_into_the_run():
    from hk_events import orchestrator

    assert "aitinkerers" in orchestrator.enabled_sources()
    assert "luma_discover" in orchestrator.enabled_sources()
