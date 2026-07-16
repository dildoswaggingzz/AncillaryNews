"""
Declarative configuration for the M3 Insight Crawler's RSS sources
(README §3B / §9 M3).

Candidate feeds were sourced from `docs/dataset-catalogue.md` §10 (the M0
audit) — nothing here is invented. Each candidate was probed by hand while
building this module; the results:

- **Nordic Balancing Model** (`nordicbalancingmodel.net/feed/`) — **works**.
  Standard WordPress RSS 2.0 feed, returns real `<item>` entries with
  title/link/author/pubDate. NBM is a Tier 1 source per README §6 (TSO/market
  operator announcements), so claims sourced from it are fact-eligible.
- **EnergyWatch** (`https://energywatch.com/service/RSS`, listed in the
  catalogue) — **dead as an RSS feed**. The URL resolves (HTTP 200) but
  returns the site's client-rendered Next.js HTML shell, not an RSS/XML
  document — `feedparser` reports `bozo=1` and finds zero entries. The
  catalogue itself flagged this as unverified ("check feed for actual
  article frequency"); it does not survive that check. Left out of the
  active list below; see the M3 report for detail. A working feed may exist
  under a different path on the same site, but per the brief we don't
  fabricate URLs the catalogue didn't already document.
- **Montel News** — catalogue explicitly notes "subscription model; free
  access limited" and no RSS/API URL was ever catalogued. Excluded.
- **Energinet press releases** — catalogue only speculates a feed "may"
  exist at an uncatalogued path; the plausible WordPress-style paths tried
  (`/feed/`, `/news/feed/`, `/media/news/feed/`) all 404. Excluded; flagged
  as a follow-up to find the real feed URL (or confirm Energinet doesn't
  publish RSS) in a future milestone.
- **EIA** — catalogue itself marks this "lower priority for Nordic focus".
  Not evaluated for this pass; left out of the active list.

Bottom line: exactly one catalogue-documented feed is confirmed live today.
The list below is intentionally short rather than padded with guessed URLs.
"""

from dataclasses import dataclass
from typing import Literal

SourceTier = Literal["tier1", "tier2"]


@dataclass(frozen=True)
class FeedConfig:
    """One RSS source the crawler polls."""

    name: str  # short slug used for logging + Qdrant `source` payload field
    url: str
    tier: SourceTier
    # Tier 1 (Energinet/ENTSO-E/NBM) sources are fact-eligible per README §6;
    # Tier 2 (media/analyst) sources are never asserted as bare fact, even if
    # Claude's extraction says so — see shared/claim_extractor.py.


RSS_FEEDS: list[FeedConfig] = [
    FeedConfig(
        name="Nordic Balancing Model",
        url="https://nordicbalancingmodel.net/feed/",
        tier="tier1",
    ),
]
