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
import pathlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from hk_events import config, dedupe, orchestrator
from hk_events.dedupe import collapse_cross_source, mirror_collapsed
from hk_events.errors import SourceFetchError, SourceNotConfiguredError
from hk_events.schema import Event, RelevanceResult
from hk_events.sources import _ical_common, aitinkerers, luma, luma_discover
from hk_events.sources._html_common import load_page_url

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _ld_page(*blocks: str) -> str:
    """An AI Tinkerers page carrying the CHAPTER-PAGE ANCHOR plus `blocks`.

    The anchor (a CollectionPage, or an Organization with a parentOrganization)
    is what separates "the right page, nothing scheduled" from "we were served
    some other page" — see `_is_chapter_page`. Both anchor blocks below are
    copied from the shapes the live page emits. Tests about event PARSING carry
    it so they are testing what they claim to.
    """
    anchor = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"CollectionPage",'
        '"@id":"https://hong-kong.aitinkerers.org/#city-page",'
        '"name":"AI Tinkerers Hong Kong: AI meetup for builders"}'
        "</script>"
    )
    return "<html><body>" + anchor + "".join(blocks) + "</body></html>"


def _ld(payload: str) -> str:
    return '<script type="application/ld+json">' + payload + "</script>"


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
    html = _ld_page(
        _ld('{"@type": "ItemList", "itemListElement": ['
            '{"@type":"ListItem","item":{"@type":"BlogPosting","name":"Recap"}}]}')
    )
    assert aitinkerers._parse_jsonld_events(html) == []


def test_aitinkerers_accepts_a_flattened_itemlist():
    """Live markup wraps each Event in a ListItem. Accept a bare Event too, so a
    future flattening of the page does not read as 'zero events this week'."""
    html = _ld_page(
        _ld('{"@type": "ItemList", "itemListElement": ['
            '{"@type":"Event","@id":"https://x/p/a#event","name":"Bare Event",'
            '"startDate":"2026-09-20T18:00:00+08:00"}]}')
    )
    events = aitinkerers._parse_jsonld_events(html)
    assert [e.title for e in events] == ["Bare Event"]
    assert events[0].url == "https://x/p/a"


def test_aitinkerers_finds_a_top_level_event_outside_any_itemlist():
    """schema.org does not require the ItemList wrapper, and the site could drop
    it. Descending only into ItemLists would turn that into a silent zero: the
    blocks parse, we find nothing, and `[]` is scored as a quiet week."""
    html = _ld_page(
        _ld('{"@context":"https://schema.org","@type":"Event",'
            '"@id":"https://x/p/solo#event","name":"Standalone Event",'
            '"startDate":"2026-09-20T18:00:00+08:00"}')
    )
    events = aitinkerers._parse_jsonld_events(html)
    assert [e.title for e in events] == ["Standalone Event"]


def test_aitinkerers_unwraps_a_graph_envelope():
    """`{"@context":…,"@graph":[…]}` is the other very common JSON-LD shape —
    it is what most CMS SEO plugins emit."""
    html = _ld_page(
        _ld('{"@context":"https://schema.org","@graph":['
            '{"@type":"Organization","name":"AI Tinkerers"},'
            '{"@type":"Event","@id":"https://x/p/g#event","name":"Graph Event",'
            '"startDate":"2026-09-21T18:00:00+08:00"}]}')
    )
    events = aitinkerers._parse_jsonld_events(html)
    assert [e.title for e in events] == ["Graph Event"]


def test_aitinkerers_raises_when_served_a_page_that_is_not_the_chapter_page():
    """The I5-equivalent guard, on the sibling adapter.

    A wrong page — a marketing landing, a consent interstitial, the platform-wide
    aitinkerers.org root — still serves well-formed JSON-LD. The blocks parse, no
    Event is found, and `[]` gets scored a SUCCESS: failure streak reset,
    `last_success` stamped for a page we never read. Note the block below is the
    PLATFORM Organization, exactly as it appears on the real chapter page — it is
    not an anchor precisely because it appears everywhere on the domain.
    """
    wrong_page = (
        "<html><body>"
        + _ld('{"@context":"https://schema.org","@type":"Organization",'
              '"@id":"https://aitinkerers.org/#organization","name":"AI Tinkerers",'
              '"url":"https://aitinkerers.org/"}')
        + "</body></html>"
    )
    with pytest.raises(SourceFetchError) as exc:
        aitinkerers._parse_jsonld_events(wrong_page)
    assert "chapter-page anchor" in str(exc.value)


def test_aitinkerers_anchor_accepts_either_signal():
    """Two independent signals so a rename of one does not take the source down.
    Both shapes are copied from what the live page emits."""
    collection_page = _ld(
        '{"@type":"CollectionPage","@id":"https://hong-kong.aitinkerers.org/#city-page"}'
    )
    chapter_org = _ld(
        '{"@type":"Organization","@id":"https://hong-kong.aitinkerers.org/#chapter-organization",'
        '"name":"AI Tinkerers Hong Kong",'
        '"parentOrganization":{"@id":"https://aitinkerers.org/#organization"}}'
    )
    for block in (collection_page, chapter_org):
        assert aitinkerers._parse_jsonld_events("<html><body>" + block + "</body></html>") == []


def test_the_real_page_satisfies_the_anchor():
    """Guards against an anchor that is right in principle and wrong in fact."""
    events = aitinkerers._parse_jsonld_events(_fixture("aitinkerers_home.html"))
    assert len(events) == 7


def test_aitinkerers_anchor_is_event_independent():
    """A chapter with nothing scheduled is a quiet week, not a failure — so the
    anchor must not be something the page only emits when it HAS events. This is
    why "at least one ItemList" was rejected: the live ItemLists are #events,
    #recent-talks and #testimonials, all of them accumulated content."""
    quiet = _ld_page(_ld('{"@type":"ItemList","@id":"x#events","itemListElement":[]}'))
    assert aitinkerers._parse_jsonld_events(quiet) == []
    # ...and the anchor alone, with no ItemList at all, is still a readable page.
    assert aitinkerers._parse_jsonld_events(_ld_page()) == []


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


def _next_data(payload: str) -> str:
    """A __NEXT_DATA__ document that PROVES it is the city discovery page.

    The `kind`/`place` anchor is what separates "the right page, quiet week" from
    "we were redirected somewhere else" — see `_discovery_container`. Tests that
    are about event parsing carry it so they are testing what they claim to.
    """
    envelope = {
        "props": {
            "pageProps": {
                "initialData": {
                    "kind": "discover-place",
                    "data": {
                        "place": {"api_id": "discplace-z9B5Guglh2WINA1", "name": "Hong Kong"},
                        "events": json.loads(payload),
                    },
                }
            }
        }
    }
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(envelope)
        + "</script></body></html>"
    )


def test_luma_discover_returns_empty_on_a_genuinely_quiet_page():
    assert luma_discover._parse_next_data(_next_data("[]")) == []


def test_luma_discover_finds_events_at_an_unexpected_path():
    """The walk is structural, not path-based, because __NEXT_DATA__ is an
    internal build artefact Luma can reshape without notice. The anchor gate
    proves we are on the right PAGE; it must not pin where the events sit."""
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"initialData":{"kind":"discover-place",'
        '"data":{"place":{"api_id":"discplace-z9B5Guglh2WINA1"}}}},'
        '"somethingNew":{"rows":[{"item":{"api_id":"evt-ZZZ",'
        '"name":"Moved Event","start_at":"2026-09-20T10:00:00.000Z","url":"abc123"}}]}}}'
        "</script></body></html>"
    )
    events = luma_discover._parse_next_data(html)
    assert [(e.external_id, e.url) for e in events] == [("evt-ZZZ", "https://lu.ma/abc123")]


def test_luma_discover_raises_when_redirected_to_another_valid_next_js_page():
    """IMPORTANT: a 200 with well-formed __NEXT_DATA__ is NOT proof we read the
    city page. lu.ma is actively redirecting to luma.com, and a redirect landing
    on a marketing / consent / login page still serves valid Next.js props: the
    JSON parses, the walk finds nothing, and `[]` gets scored as a SUCCESS —
    resetting the failure streak and stamping a last_success we never earned.
    That silent zero is the exact failure this branch exists to prevent."""
    marketing = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"marketing":true}}}'
        "</script></body></html>"
    )
    with pytest.raises(SourceFetchError) as exc:
        luma_discover._parse_next_data(marketing)
    assert "discovery-listing container" in str(exc.value)


@pytest.mark.parametrize("api_id", ["cal-ABC123", "tix-Q1", "discplace-z9B5", "usr-EmFxsO7"])
def test_luma_discover_skips_non_event_objects_that_share_the_shape(api_id):
    """The walk keys off "has name AND start_at", which is a shape, not a type.
    Luma hangs calendars, ticket types, places and users off ids in the same tree
    and some carry both fields. Only `evt-` is an event — and only `evt-` can
    collide with the .ics adapter. A phantom here would reach the digest AND the
    calendar."""
    payload = (
        f'[{{"api_id":"{api_id}","name":"Not An Event",'
        '"start_at":"2026-09-20T10:00:00.000Z","url":"x"}]'
    )
    assert luma_discover._parse_next_data(_next_data(payload)) == []


def test_luma_discover_keeps_evt_objects_alongside_the_phantoms():
    """The prefix guard must not be so eager it drops real events."""
    payload = (
        '[{"api_id":"cal-ABC","name":"A Calendar","start_at":"2026-09-20T10:00:00.000Z"},'
        '{"api_id":"evt-REAL","name":"A Real Event","start_at":"2026-09-20T10:00:00.000Z","url":"r"}]'
    )
    events = luma_discover._parse_next_data(_next_data(payload))
    assert [e.external_id for e in events] == ["evt-REAL"]


def test_luma_discover_skips_an_event_with_no_api_id():
    """Without an api_id there is no stable id and no way to collide with the
    .ics adapter — it would be re-notified every run AND double-reported."""
    payload = '[{"name":"Anonymous","start_at":"2026-09-20T10:00:00.000Z"}]'
    assert luma_discover._parse_next_data(_next_data(payload)) == []


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


class TestOnlyTheLumaAdaptersMayClaimTheLumaNamespace:
    """`identity_key` is the one place in the pipeline where an event can be
    silently DROPPED rather than duplicated.

    `collapse_cross_source` keeps one row per `identity_key` and
    `_SOURCE_PRECEDENCE` picks the winner — so an event carrying
    `canonical_id="luma-evt:evt-X"` from a source that is not a Luma adapter
    would collide with the real Luma event and, outranking it, take its place.
    Nothing logs a drop; the digest is just one event short. Unreachable today
    (only `luma` and `luma_discover` write the key), which is exactly why it
    needs a guard: the next adapter's author has no reason to know.

    Rejection always falls back to `dedup_key`, the safe direction — worst case
    the event is reported twice, never zero times.
    """

    def _impostor(self, source, canonical):
        return Event(
            source=source,
            external_id="whatever",
            title="Impostor",
            url="https://example.com/impostor",
            raw={"canonical_id": canonical},
        )

    def test_a_foreign_source_cannot_squat_the_luma_namespace(self):
        ev = self._impostor("meetup", f"luma-evt:{_OVERLAP_API_ID}")
        assert ev.identity_key == ev.dedup_key
        assert ev.identity_key != f"luma-evt:{_OVERLAP_API_ID}"

    def test_the_squatter_no_longer_displaces_the_real_luma_event(self):
        """The consequence, through the real collapse.

        `meetup` outranks BOTH luma sources in `_SOURCE_PRECEDENCE`, so without
        the guard the impostor won the merge and the genuine event vanished from
        `kept` entirely — the digest silently one event short. The luma pair
        must still collapse into one another, so the correct outcome is exactly
        two survivors: the impostor, standing alone, and one real Luma row.
        """
        from_ics, from_page = _luma_pair()
        impostor = self._impostor("meetup", f"luma-evt:{_OVERLAP_API_ID}")
        kept, collapsed = collapse_cross_source([from_ics, from_page, impostor])

        assert impostor in kept, "the impostor must survive as its own event"
        real = [e for e in kept if e.source in {"luma", "luma_discover"}]
        assert len(real) == 1, f"the genuine Luma event was dropped: {kept}"
        assert len(kept) == 2, f"expected impostor + one Luma row, got {kept}"
        # The only merge is the legitimate one, between the two Luma adapters.
        assert [(w.source, d.source) for w, d in collapsed] == [("luma", "luma_discover")]

    def test_both_genuine_luma_adapters_still_collide(self):
        """Premise. A guard that rejected the owners too would silently undo the
        cross-source dedupe this branch built."""
        from_ics, from_page = _luma_pair()
        assert from_ics.identity_key == from_page.identity_key

    @pytest.mark.parametrize("value", [True, 1, ["luma-evt:x"], {"a": 1}])
    def test_a_non_string_canonical_id_is_refused_not_coerced(self, value):
        """`str(True)` is `"True"`, and every event carrying it would merge into
        one. There is no legitimate non-string canonical_id."""
        ev = self._impostor("luma", value)
        assert ev.identity_key == ev.dedup_key


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


# ---------------------------------------------------------------------------
# END-TO-END: the two halves joined
#
# The tests above proved `_uid_canonical_id` maps a UID correctly, and proved
# `collapse_cross_source` merges Events that already carry a matching
# `raw["canonical_id"]`. Both halves passed in ISOLATION while the wiring between
# them could be deleted without a single failure — review confirmed that
# `luma.py`'s `canonical_id=_uid_canonical_id` could be changed to `None`, and
# that the orchestrator's collapse call could be replaced with `collapsed = []`,
# and the suite stayed green either way.
#
# These tests join them: real .ics bytes in one side, the real city page in the
# other, one survivor out.
# ---------------------------------------------------------------------------

_ICS_FIXTURE = "luma_startupshk_calendar.ics"
# The genuine overlap, observed live 2026-09-01: this event was on the startupshk
# calendar feed AND on lu.ma/hong-kong at the same time.
_OVERLAP_API_ID = "evt-cuDFACZOa8zGKRu"


@pytest.fixture
def luma_ics_feed(monkeypatch, real_sources):
    """Serve the captured .ics through the REAL `luma.fetch_luma_events` path.

    Horizon filtering is neutralised because the fixture is verbatim captured
    bytes — the overlapping event is dated 2026-09-05, so a real horizon check
    would make this test start passing vacuously (0 events, 0 collapses) the
    moment that date goes by. Rewriting the fixture's DTSTART to a rolling future
    date was the alternative and is worse: a fixture edited until the test passes
    is not evidence about the real feed. This test is about the collapse join,
    not about the window.
    """
    real_sources()  # these tests drive the genuine adapters, not conftest's stubs
    monkeypatch.setattr(_ical_common, "_within_horizon", lambda start: True)
    monkeypatch.setattr(_ical_common, "load_feed_urls", lambda group: ["https://api.lu.ma/ics/get?id=cal-x"])

    class _Resp:
        status_code = 200
        text = (FIXTURES / _ICS_FIXTURE).read_text()

    monkeypatch.setattr(httpx.Client, "get", lambda *a, **k: _Resp())


def test_the_ics_adapter_really_stamps_a_canonical_id(luma_ics_feed):
    """`luma.fetch_luma_events` end-to-end, not `_uid_canonical_id` in a vacuum.

    Kills the mutation `canonical_id=_uid_canonical_id` → `canonical_id=None`.
    """
    events = luma.fetch_luma_events()
    by_uid = {e.external_id: e for e in events}

    overlapping = by_uid[f"{_OVERLAP_API_ID}@events.lu.ma"]
    assert overlapping.raw["canonical_id"] == f"luma-evt:{_OVERLAP_API_ID}"
    assert overlapping.identity_key == f"luma-evt:{_OVERLAP_API_ID}"

    # The `calev-` row in the same feed gets none, and falls back to dedup_key.
    calendar_row = by_uid["calev-1huzvBb6z8QJmNv@events.lu.ma"]
    assert "canonical_id" not in calendar_row.raw
    assert calendar_row.identity_key == calendar_row.dedup_key


def test_real_ics_and_real_city_page_collapse_to_one_survivor(luma_ics_feed):
    """The whole point of the feature, through both real adapters and both real
    fixtures. No hand-stuffed `raw` dicts anywhere in this test."""
    from_ics = luma.fetch_luma_events()
    from_page = luma_discover._parse_next_data(_fixture("luma_hong_kong.html"))

    # Precondition: the same event really is in both fixtures, by two different
    # external_ids. If this ever stops holding, the assertions below are vacuous.
    assert f"{_OVERLAP_API_ID}@events.lu.ma" in {e.external_id for e in from_ics}
    assert _OVERLAP_API_ID in {e.external_id for e in from_page}

    kept, collapsed = collapse_cross_source(from_ics + from_page, seen_lookup=lambda s: {})

    assert len(kept) == len(from_ics) + len(from_page) - 1
    assert [w.identity_key for w, _ in collapsed] == [f"luma-evt:{_OVERLAP_API_ID}"]
    survivors = [e for e in kept if e.identity_key == f"luma-evt:{_OVERLAP_API_ID}"]
    assert len(survivors) == 1
    assert survivors[0].title == "Paperclip-maxxing Capitalism"


def test_the_orchestrator_collapses_before_the_seen_set_diff(monkeypatch, tmp_path, luma_ics_feed):
    """Pins the ORDER, not just the existence, of the collapse.

    `filter_due` keeps a separate seen-set per source, so a duplicate that reaches
    it is diffed twice, classified twice, and calendared twice. Running the
    collapse after it would be useless. Kills the mutation
    `events, collapsed = collapse_cross_source(events)` → `collapsed = []`.
    """
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dedupe, "STATE_DIR", tmp_path)

    page_events = luma_discover._parse_next_data(_fixture("luma_hong_kong.html"))
    monkeypatch.setattr(
        orchestrator.luma_discover, "fetch_luma_discover_events", lambda: page_events
    )
    # meetup and aitinkerers back to a clean zero: `luma_ics_feed` patches
    # `load_feed_urls` and `httpx.Client.get` globally, so an un-stubbed meetup
    # would parse the Luma .ics fixture as its own events.
    monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", lambda: [])
    monkeypatch.setattr(orchestrator.aitinkerers, "fetch_aitinkerers_events", lambda: [])

    seen_by_filter: list[Event] = []
    real_filter_due = orchestrator.filter_due

    def _spy(events, **kwargs):
        seen_by_filter.extend(events)
        return real_filter_due(events, **kwargs)

    monkeypatch.setattr(orchestrator, "filter_due", _spy)
    monkeypatch.setattr(
        orchestrator, "classify", lambda e: RelevanceResult(tag="founder_ai", reason="test")
    )

    assert orchestrator.run(dry_run=True) == 0

    reaching_filter = [e for e in seen_by_filter if e.identity_key == f"luma-evt:{_OVERLAP_API_ID}"]
    assert len(reaching_filter) == 1, (
        "the duplicate reached filter_due twice — the collapse either did not run "
        "or ran after the seen-set diff"
    )


# ---------------------------------------------------------------------------
# The cross-source hand-off: what happens when the WINNER stops reporting
# ---------------------------------------------------------------------------

class TestSeenSetSurvivesTheHandOff:
    """`collapse_cross_source` picks one survivor, so only the winner's source
    reaches `filter_due` — and `filter_due` builds its seen-sets from the events
    it iterates, so the LOSER's state file is never written. The winner is stable
    only while both sources keep reporting.

    They don't. The city page is a bounded listing of the next ~12 events; the
    .ics horizon is 45 days. An event ageing off the listing while still on a
    followed calendar is the NORMAL life cycle, and on that run `luma` wins by
    default, finds nothing in `seen_luma.json`, and re-notifies — re-pushing to
    Telegram and writing a SECOND calendar entry, because `stable_hash` mixes the
    source into the digest.

    `mirror_collapsed` closes it by writing the winner's record into the loser's
    seen-set as well. These tests walk the exact four-run sequence.
    """

    API_ID = "evt-handoff01"
    START = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)

    def _pair(self):
        canonical = f"luma-evt:{self.API_ID}"
        ics = Event(
            source="luma",
            external_id=f"{self.API_ID}@events.lu.ma",
            title="Hand-off Test Event",
            url="https://luma.com/handoff",
            start=self.START,
            raw={"uid": f"{self.API_ID}@events.lu.ma", "canonical_id": canonical},
        )
        page = Event(
            source="luma_discover",
            external_id=self.API_ID,
            title="Hand-off Test Event",
            url="https://lu.ma/handoff",
            start=self.START,
            raw={"api_id": self.API_ID, "canonical_id": canonical},
        )
        return ics, page

    @staticmethod
    def _run(events: list[Event], state: pathlib.Path, monkeypatch) -> tuple[list, list]:
        """One full run's worth of dedupe: collapse → diff → verdict → mirror →
        persist. Mirrors `orchestrator.run` steps 1d through 3b and 6."""
        monkeypatch.setattr(config, "STATE_DIR", state)
        monkeypatch.setattr(dedupe, "STATE_DIR", state)
        kept, collapsed = collapse_cross_source(events)
        due, seen_by_source = dedupe.filter_due(kept)
        for event, _stage, _tag in due:
            dedupe.record_verdict(seen_by_source, event, "founder_ai")
        mirror_collapsed(seen_by_source, collapsed)
        for source, seen in seen_by_source.items():
            dedupe.save_seen(source, seen)
        return kept, [(e.source, stage) for e, stage, _ in due]

    def test_the_event_is_notified_exactly_once_across_the_hand_off(self, tmp_path, monkeypatch):
        ics, page = self._pair()

        # run 1 — standalone: only the city page has it.
        kept, due = self._run([page], tmp_path, monkeypatch)
        assert [e.source for e in kept] == ["luma_discover"]
        assert due == [("luma_discover", "new")]

        # run 2 — the host attaches it to a followed calendar. Both sources now
        # carry it; continuity keeps luma_discover as the winner.
        kept, due = self._run([ics, page], tmp_path, monkeypatch)
        assert [e.source for e in kept] == ["luma_discover"]
        assert due == []

        # run 3 — THE REGRESSION. It ages off the 12-event city listing but is
        # still inside the 45-day .ics horizon, so `luma` wins by default.
        kept, due = self._run([ics], tmp_path, monkeypatch)
        assert [e.source for e in kept] == ["luma"]
        assert due == [], "re-notified on the hand-off — the loser's seen-set was never written"

        # run 4 — back on the listing (a host bump, a recurrence). Still silent.
        kept, due = self._run([ics, page], tmp_path, monkeypatch)
        assert due == []

    def test_the_mirror_is_what_writes_the_losers_state_file(self, tmp_path, monkeypatch):
        """Names the mechanism, so a future refactor that drops the mirror fails
        here with a readable reason rather than only in the sequence above."""
        ics, page = self._pair()
        self._run([page], tmp_path, monkeypatch)
        assert not (tmp_path / "seen_luma.json").exists()

        self._run([ics, page], tmp_path, monkeypatch)
        seen_luma = json.loads((tmp_path / "seen_luma.json").read_text())
        assert ics.external_id in seen_luma
        # The verdict rides along, so the loser's source will not resurface an
        # event the classifier already rejected.
        assert seen_luma[ics.external_id]["tag"] == "founder_ai"

    def _settle(self, events, state, monkeypatch, *, verdict="founder_ai"):
        """collapse → diff → verdict → mirror → persist, returning the winner."""
        monkeypatch.setattr(config, "STATE_DIR", state)
        monkeypatch.setattr(dedupe, "STATE_DIR", state)
        kept, collapsed = collapse_cross_source(events)
        due, seen_by_source = dedupe.filter_due(kept)
        for event, _stage, _tag in due:
            dedupe.record_verdict(seen_by_source, event, verdict)
        mirror_collapsed(seen_by_source, collapsed)
        for source, seen in seen_by_source.items():
            dedupe.save_seen(source, seen)
        return kept, collapsed

    def test_a_dropped_verdict_is_mirrored_too(self, tmp_path, monkeypatch):
        """A "drop" that only reached the winner's file would let the loser's
        source resurface an event the filter already rejected.

        ⚠️ ASSERT ON THE LOSER'S FILE. An earlier version of this test read
        `seen_luma.json` — but `luma` is the WINNER here, and `filter_due` +
        `record_verdict` write the winner's record whether or not the mirror runs
        at all. It passed with `mirror_collapsed` a complete no-op, i.e. it named
        a guarantee it did not enforce.
        """
        ics, page = self._pair()
        kept, collapsed = self._settle([ics, page], tmp_path, monkeypatch, verdict="drop")

        # Precondition: without this, the assertion below is about the wrong file.
        assert [e.source for e in kept] == ["luma"]
        assert [loser.source for _w, loser in collapsed] == ["luma_discover"]

        seen_loser = json.loads((tmp_path / "seen_luma_discover.json").read_text())
        assert seen_loser[page.external_id]["tag"] == "drop"

    def test_the_mirror_does_not_re_arm_a_reminder_already_fired(self, tmp_path, monkeypatch):
        """The MERGE branch of `mirror_collapsed`, which was entirely unguarded.

        When the loser's side already tracks the event and has fired its reminder,
        the mirror must merge into that record, not replace it. A blind overwrite
        would reset the loser's `stages` from ["new","soon"] back to ["new"] and
        re-arm a reminder that already went out.

        Both sources are seeded so both are "already seen" — that keeps the
        continuity rule from flipping the winner, so the fixed precedence decides
        (`luma` wins) and the LOSER is `luma_discover`, which is the side carrying
        the fired reminder and therefore the side the merge branch has to protect.
        """
        ics, page = self._pair()
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dedupe, "STATE_DIR", tmp_path)
        # Winner's record: discovery only.
        dedupe.save_seen("luma", {ics.external_id: {"stages": ["new"], "tag": "founder_ai"}})
        # Loser's record: reminder ALREADY fired.
        dedupe.save_seen(
            "luma_discover",
            {page.external_id: {"stages": ["new", "soon"], "tag": "founder_ai"}},
        )

        kept, collapsed = collapse_cross_source([ics, page])
        assert [e.source for e in kept] == ["luma"]
        assert [loser.source for _w, loser in collapsed] == ["luma_discover"]

        due, seen_by_source = dedupe.filter_due(kept)
        mirror_collapsed(seen_by_source, collapsed)
        for source, seen in seen_by_source.items():
            dedupe.save_seen(source, seen)

        seen_loser = json.loads((tmp_path / "seen_luma_discover.json").read_text())
        assert seen_loser[page.external_id]["stages"] == ["new", "soon"], (
            "the mirror overwrote the loser's record instead of merging into it — "
            "an already-fired reminder has been re-armed"
        )

    def test_the_mirror_adds_a_stage_the_loser_has_not_seen(self, tmp_path, monkeypatch):
        """The merge is a union, not a freeze: a stage the WINNER has fired and
        the loser has not must still propagate, or the loser's source would
        re-fire it after a hand-off."""
        ics, page = self._pair()
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dedupe, "STATE_DIR", tmp_path)
        dedupe.save_seen(
            "luma", {ics.external_id: {"stages": ["new", "soon"], "tag": "founder_ai"}}
        )
        dedupe.save_seen(
            "luma_discover", {page.external_id: {"stages": ["new"], "tag": None}}
        )

        _kept, collapsed = collapse_cross_source([ics, page])
        seen_by_source = {
            "luma": dedupe.load_seen("luma"),
            "luma_discover": dedupe.load_seen("luma_discover"),
        }
        mirror_collapsed(seen_by_source, collapsed)

        rec = seen_by_source["luma_discover"][page.external_id]
        assert rec["stages"] == ["new", "soon"]
        assert rec["tag"] == "founder_ai"

    # ------------------------------------------------------------------
    # ...and the same sequence through the REAL orchestrator.
    #
    # The four-run test above drives the dedupe functions directly, which is
    # readable but reproduces the exact isolation failure this review caught
    # elsewhere: deleting `mirror_collapsed(...)` from `orchestrator.run` left
    # that test green, because the test re-implemented the call itself. This one
    # runs the actual pipeline, so the WIRING is under test, not just the
    # function.
    # ------------------------------------------------------------------

    @pytest.fixture
    def orchestrated(self, monkeypatch, tmp_path):
        """Run `orchestrator.run` for real, minus delivery.

        `dry_run=True` cannot be used: the seen-set save lives in the non-dry-run
        branch, and persistence is precisely what is under test here. So the run
        is real and only its outbound edges are stubbed.
        """
        monkeypatch.setattr(config, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dedupe, "STATE_DIR", tmp_path)
        monkeypatch.setattr(orchestrator.config, "assert_required", lambda: None)
        monkeypatch.setattr(orchestrator, "write_archive", lambda today, md: None)
        monkeypatch.setattr(orchestrator, "sync_events", lambda events, dry_run: None)
        monkeypatch.setattr(orchestrator.source_health, "save_health", lambda health: None)
        monkeypatch.setattr(
            orchestrator, "classify", lambda e: RelevanceResult(tag="founder_ai", reason="test")
        )

        # RECORD the pushes rather than discarding them. A stub that swallows
        # them makes "did not re-push" unobservable — the state-file assertions
        # a re-pushing run leaves behind are byte-identical to a silent one's.
        pushes: list[list[str]] = []
        monkeypatch.setattr(orchestrator, "push_messages", lambda messages: pushes.append(messages))

        def _run(*, ics: list[Event], page: list[Event]) -> list[list[str]]:
            """Run once; return the pushes THIS run made."""
            monkeypatch.setattr(orchestrator.meetup, "fetch_meetup_events", lambda: [])
            monkeypatch.setattr(orchestrator.aitinkerers, "fetch_aitinkerers_events", lambda: [])
            monkeypatch.setattr(orchestrator.luma, "fetch_luma_events", lambda: list(ics))
            monkeypatch.setattr(
                orchestrator.luma_discover, "fetch_luma_discover_events", lambda: list(page)
            )
            before = len(pushes)
            assert orchestrator.run() == 0
            return pushes[before:]

        return _run

    def test_the_orchestrator_does_not_re_push_across_the_hand_off(
        self, orchestrated, tmp_path
    ):
        ics, page = self._pair()

        # run 1 — standalone. It is genuinely new, so it SHOULD push. Asserting
        # this is what makes the run-3 assertion mean something: it proves the
        # counter can see a push at all.
        run1 = orchestrated(ics=[], page=[page])
        assert len(run1) == 1
        assert page.title in "\n".join(run1[0])
        assert self.API_ID in json.loads((tmp_path / "seen_luma_discover.json").read_text())

        # run 2 — both sources carry it. The only run that can teach `luma` the
        # event exists, and it only does so because orchestrator.run calls
        # `mirror_collapsed`. Nothing new, so nothing pushed.
        run2 = orchestrated(ics=[ics], page=[page])
        assert run2 == []
        seen_luma = json.loads((tmp_path / "seen_luma.json").read_text())
        assert ics.external_id in seen_luma, (
            "orchestrator.run did not mirror the collapsed event into the loser's "
            "seen-set — the next run will re-push it and double-book the calendar"
        )

        # run 3 — THE REGRESSION. It ages off the city listing, `luma` wins by
        # default, and must stay silent. This is the assertion the test's title
        # claims: zero pushes, observed, not inferred from a state file that a
        # re-pushing run would have left looking identical.
        run3 = orchestrated(ics=[ics], page=[])
        assert run3 == [], (
            f"re-pushed on the hand-off: {run3!r} — the loser's seen-set did not "
            "carry the event, so `luma` treated it as newly discovered"
        )
        after = json.loads((tmp_path / "seen_luma.json").read_text())
        assert after[ics.external_id]["stages"] == ["new"]
        assert after[ics.external_id]["tag"] == "founder_ai"

        # run 4 — back on the listing. Still silent.
        assert orchestrated(ics=[ics], page=[page]) == []
