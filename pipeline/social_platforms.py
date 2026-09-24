"""
Scout — organic social: what to ask each Apify actor, and how to read what comes back.

Pure: no network, no database. collect_social.py does the I/O.

Same actors and input shapes as the internal tool's collectors/social_apify.py, which
has been running since May. What changed, and why, measured against the 1,086 rows it
has written to public.signals:

  FOLLOWERS were null on 100% of Facebook, TikTok and YouTube rows and 64% of Instagram
    rows. The field names were wrong: TikTok carries `authorMeta.fans` on every video,
    YouTube `numberOfSubscribers` on every video, and Instagram's posts output has no
    follower count at all (it needs the `details` result type). Facebook's posts output
    has none, so it is recorded as unknown rather than guessed.

  POST COUNTS were capped by resultsLimit and reported as counts: Instagram's
    posts_last_30d reads exactly 20 constantly, which is the limit, the same defect as
    the internal ad collector's 35. Here a channel that returns as many posts as were
    asked for is flagged `capped` and its count is a floor.

  THE WINDOW was a trailing 30 days on a weekly run, so each week shared three quarters
    of its posts with the last and the series could barely move. Here the window is the
    seven days before the briefing week, [W-7, W), filtered at the actor where the
    actor supports it and always again here. Weeks do not overlap.

X had no collector internally. apidojo/tweet-scraper, the most used X actor on Apify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

LIMIT = 50  # posts per channel per week. A branch posting more than this is capped.


def limit_for(weeks: int) -> int:
    return LIMIT * max(1, weeks)

ACTORS = {
    "instagram": "apify/instagram-scraper",
    "facebook": "apify/facebook-posts-scraper",
    "tiktok": "clockworks/tiktok-scraper",
    "youtube": "streamers/youtube-scraper",
    "x": "apidojo/tweet-scraper",
}


@dataclass
class Post:
    platform: str
    post_id: str
    posted_at: datetime
    text: str
    url: str | None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    format: str | None = None
    is_pinned: bool = False
    is_repost: bool = False
    followers: int | None = None     # when the platform puts it on each post
    owner: str | None = None         # handle / channel id, for matching back
    input_url: str | None = None


@dataclass
class Channel:
    id: str
    competitor_id: str
    platform: str
    scope: str
    handle: str | None
    external_id: str | None = None
    url: str | None = None
    location_label: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def page_url(self) -> str:
        p, h, e = self.platform, self.handle, self.external_id
        if p == "instagram":
            return f"https://www.instagram.com/{h}/"
        if p == "facebook":
            return f"https://www.facebook.com/{h or e}"
        if p == "tiktok":
            return f"https://www.tiktok.com/@{h}"
        if p == "youtube":
            return f"https://www.youtube.com/channel/{e}/videos"
        if p == "x":
            return f"https://x.com/{h}"
        raise ValueError(p)


# ── time ─────────────────────────────────────────────────────────────────────

_REL = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_REL_UNIT = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24, "day": 1,
             "week": 7, "month": 30, "year": 365}


def parse_time(v: Any, now: datetime | None = None) -> datetime | None:
    """Every timestamp shape these five actors emit: ISO strings with or without Z,
    epoch seconds or milliseconds, Twitter's "Mon Sep 22 14:01:02 +0000 2026", and
    YouTube's relative "3 days ago". Always returns an aware UTC datetime or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000
        try:
            return datetime.fromtimestamp(x, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if s.isdigit():
        return parse_time(int(s), now)
    m = _REL.search(s)
    if m:
        now = now or datetime.now(timezone.utc)
        return now - timedelta(days=int(m.group(1)) * _REL_UNIT[m.group(2).lower()])
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def window(wk: date, weeks: int = 1) -> tuple[datetime, datetime]:
    """[W-7, W) in UTC: the seven days before the briefing week. With weeks > 1, the
    span a backfill covers: [W - 7*weeks, W)."""
    end = datetime(wk.year, wk.month, wk.day, tzinfo=timezone.utc)
    return end - timedelta(days=7 * weeks), end


def _int(v: Any) -> int | None:
    try:
        return None if v is None or v == "" else int(float(v))
    except (TypeError, ValueError):
        return None


def _norm(h: str | None) -> str:
    return (h or "").strip().lower().lstrip("@").rstrip("/")


# ── payloads ─────────────────────────────────────────────────────────────────

def payload(platform: str, channels: list[Channel], wk: date, weeks: int = 1) -> dict[str, Any]:
    since, until = window(wk, weeks)
    d = since.date().isoformat()
    LIMIT = limit_for(weeks)
    if platform == "instagram":
        return {"directUrls": [c.page_url for c in channels], "resultsType": "posts",
                "resultsLimit": LIMIT, "onlyPostsNewerThan": d, "addParentData": True}
    if platform == "facebook":
        return {"startUrls": [{"url": c.page_url} for c in channels],
                "resultsLimit": LIMIT, "onlyPostsNewerThan": d,
                "onlyPostsOlderThan": until.date().isoformat()}
    if platform == "tiktok":
        # The actor's date filter is a paid add-on; sort newest first and cut here.
        return {"profiles": [c.handle for c in channels], "resultsPerPage": LIMIT,
                "profileSorting": "latest", "excludePinnedPosts": True,
                "shouldDownloadVideos": False, "shouldDownloadCovers": False}
    if platform == "youtube":
        return {"startUrls": [{"url": c.page_url} for c in channels],
                "maxResults": LIMIT, "maxResultsShorts": LIMIT, "maxResultStreams": 0,
                "oldestPostDate": d, "sortVideosBy": "NEWEST"}
    if platform == "x":
        return {"twitterHandles": [c.handle for c in channels],
                "maxItems": LIMIT * len(channels), "sort": "Latest",
                "start": d, "end": until.date().isoformat()}
    raise ValueError(platform)


def details_payload(channels: list[Channel]) -> dict[str, Any]:
    """Instagram only: the profile call that carries followersCount."""
    return {"directUrls": [c.page_url for c in channels], "resultsType": "details",
            "resultsLimit": 1}


# ── normalizers: one actor item to one Post, or None ─────────────────────────

def _instagram(i: dict[str, Any]) -> Post | None:
    t = parse_time(i.get("timestamp"))
    pid = i.get("id") or i.get("shortCode")
    if not t or not pid or i.get("error"):
        return None
    return Post("instagram", str(pid), t, i.get("caption") or "", i.get("url"),
                likes=_int(i.get("likesCount")), comments=_int(i.get("commentsCount")),
                views=_int(i.get("videoPlayCount") or i.get("videoViewCount")),
                format=(i.get("productType") or i.get("type") or "").lower() or None,
                is_pinned=bool(i.get("isPinned")), owner=i.get("ownerUsername"),
                input_url=i.get("inputUrl"))


def _facebook(i: dict[str, Any]) -> Post | None:
    t = parse_time(i.get("time")) or parse_time(i.get("timestamp"))
    pid = i.get("postId") or i.get("id")
    if not t or not pid or i.get("error"):
        return None
    return Post("facebook", str(pid), t, i.get("text") or "", i.get("url") or i.get("topLevelUrl"),
                likes=_int(i.get("likes")), comments=_int(i.get("comments")),
                shares=_int(i.get("shares")),
                views=_int(i.get("viewsCount") or i.get("videoPostViewCount")),
                format="video" if i.get("isVideo") else "post",
                is_repost=bool(i.get("sharedPost")),
                owner=i.get("pageName") if isinstance(i.get("pageName"), str) else None,
                input_url=i.get("inputUrl") or i.get("facebookUrl"))


def _tiktok(i: dict[str, Any]) -> Post | None:
    t = parse_time(i.get("createTimeISO")) or parse_time(i.get("createTime"))
    pid = i.get("id")
    if not t or not pid:
        return None
    a = i.get("authorMeta") or {}
    return Post("tiktok", str(pid), t, i.get("text") or "", i.get("webVideoUrl"),
                likes=_int(i.get("diggCount")), comments=_int(i.get("commentCount")),
                shares=_int(i.get("shareCount")), views=_int(i.get("playCount")),
                format="slideshow" if i.get("isSlideshow") else "video",
                is_pinned=bool(i.get("isPinned")), followers=_int(a.get("fans")),
                owner=a.get("name"), input_url=i.get("input"))


def _youtube(i: dict[str, Any], now: datetime | None = None) -> Post | None:
    t = parse_time(i.get("date"), now)
    pid = i.get("id")
    if not t or not pid or i.get("error"):
        return None
    text = " ".join(x for x in (i.get("title"), i.get("text")) if x)
    return Post("youtube", str(pid), t, text, i.get("url"),
                likes=_int(i.get("likes")), comments=_int(i.get("commentsCount")),
                views=_int(i.get("viewCount")), format=(i.get("type") or "video").lower(),
                followers=_int(i.get("numberOfSubscribers")),
                owner=i.get("channelId"), input_url=i.get("inputChannelUrl") or i.get("input"))


def _x(i: dict[str, Any]) -> Post | None:
    t = parse_time(i.get("createdAt"))
    pid = i.get("id")
    if not t or not pid or i.get("noResults"):
        return None
    a = i.get("author") or {}
    return Post("x", str(pid), t, i.get("fullText") or i.get("text") or "",
                i.get("url") or i.get("twitterUrl"),
                likes=_int(i.get("likeCount")), comments=_int(i.get("replyCount")),
                shares=_int(i.get("retweetCount")), views=_int(i.get("viewCount")),
                format="reply" if i.get("isReply") else "post",
                is_repost=bool(i.get("isRetweet")), followers=_int(a.get("followers")),
                owner=a.get("userName"))


NORMALIZE: dict[str, Callable[..., Post | None]] = {
    "instagram": _instagram, "facebook": _facebook, "tiktok": _tiktok,
    "youtube": _youtube, "x": _x,
}


# ── matching an item back to the channel that asked for it ───────────────────

def match(post: Post, channels: list[Channel]) -> Channel | None:
    """Owner handle first, then the input URL the actor echoes back. A post that
    matches no channel is dropped: counting it would put a stranger's posts in a
    competitor's number."""
    owner = _norm(post.owner)
    for c in channels:
        keys = {_norm(c.handle), _norm(c.external_id)} - {""}
        if owner and owner in keys:
            return c
    inp = _norm(post.input_url)
    if inp:
        for c in channels:
            for k in (_norm(c.handle), _norm(c.external_id)):
                if k and (inp.endswith("/" + k) or inp.endswith("@" + k)
                          or f"/{k}/" in inp + "/" or f"@{k}/" in inp + "/"):
                    return c
    if len(channels) == 1:
        return channels[0]
    return None


def followers_from_details(items: list[dict[str, Any]], channels: list[Channel]) -> dict[str, int]:
    """Instagram details items to {channel_id: followers}."""
    out: dict[str, int] = {}
    for i in items:
        u = _norm(i.get("username"))
        n = _int(i.get("followersCount"))
        for c in channels:
            if u and u == _norm(c.handle) and n is not None:
                out[c.id] = n
    return out
