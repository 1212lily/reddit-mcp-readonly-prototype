"""Offline parsing, source shaping, and request policy. No network client lives here."""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import urlparse

from .models import Comment, Pagination, Post, ResearchSource, Status


POST_ID = re.compile(r"^[a-z0-9]{1,20}$")
COMMENT_ID = re.compile(r"^[a-z0-9]{1,20}$")
SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")
USERNAME = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


class RedditFailure(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class SecretProvider(Protocol):
    """Future approved integration injects values; never log or return them."""

    def get(self, name: str) -> str | None: ...


class OAuthClient(Protocol):
    """Future approved integration supplies authorization without exposing tokens."""

    async def authorization_header(self) -> Mapping[str, str]: ...


class ReadClient(Protocol):
    """A permitted API client or an offline fixture client."""

    async def read(self, route: str, params: Mapping[str, str | int] | None = None) -> Any: ...


def validate_subreddit(name: str) -> str:
    if not SUBREDDIT.fullmatch(name):
        raise ValueError("Invalid subreddit name")
    return name


def validate_username(name: str) -> str:
    if not USERNAME.fullmatch(name):
        raise ValueError("Invalid username")
    return name


def validate_id(value: str, *, comment: bool = False) -> str:
    value = value.removeprefix("t1_" if comment else "t3_")
    if not (COMMENT_ID if comment else POST_ID).fullmatch(value):
        raise ValueError("Invalid Reddit ID")
    return value


def post_id_from_reference(post_id: str | None, post_url: str | None) -> str:
    if (post_id is None) == (post_url is None):
        raise ValueError("Provide exactly one of post_id or post_url")
    if post_id is not None:
        return validate_id(post_id)
    parsed = urlparse(post_url or "")
    if parsed.scheme != "https" or parsed.hostname not in {"reddit.com", "www.reddit.com", "redd.it"}:
        raise ValueError("Only HTTPS Reddit post URLs are allowed")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("URL credentials and custom ports are not allowed")
    if parsed.hostname == "redd.it":
        match = re.fullmatch(r"/([a-z0-9]{1,20})/?", parsed.path)
    else:
        match = re.fullmatch(r"/r/[A-Za-z0-9_]{2,21}/comments/([a-z0-9]{1,20})(?:/[^/]*)?/?", parsed.path)
    if not match:
        raise ValueError("Not a Reddit post URL")
    return validate_id(match.group(1))


def username_from_reference(username: str | None, profile_url: str | None) -> str:
    if (username is None) == (profile_url is None):
        raise ValueError("Provide exactly one of username or profile_url")
    if username is not None:
        return validate_username(username)
    parsed = urlparse(profile_url or "")
    if (parsed.scheme != "https" or parsed.hostname not in {"reddit.com", "www.reddit.com"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Only HTTPS Reddit profile URLs are allowed")
    try:
        if parsed.port is not None:
            raise ValueError("Custom ports are not allowed")
    except ValueError as error:
        raise ValueError("Invalid Reddit profile URL port") from error
    match = re.fullmatch(r"/user/([A-Za-z0-9_-]{3,20})/?", parsed.path)
    if not match:
        raise ValueError("Invalid Reddit profile URL")
    return validate_username(match.group(1))


def utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def source(kind: str, permalink: str, now: str, status: Status, *, subreddit: str | None = None,
           post_id: str | None = None, comment_id: str | None = None) -> ResearchSource:
    if not permalink.startswith("/") or permalink.startswith("//") or "?" in permalink or "#" in permalink:
        raise RedditFailure("INVALID_PERMALINK")
    return ResearchSource(source_type=kind, subreddit=subreddit, post_id=post_id,
                          comment_id=comment_id, permalink="https://www.reddit.com" + permalink,
                          retrieved_at=now, status=status)


def parse_post(raw: Mapping[str, Any], now: str) -> Post:
    try:
        post_id = validate_id(raw["id"])
        subreddit = validate_subreddit(raw["subreddit"])
        permalink = raw["permalink"]
        title = raw.get("title")
        body = raw.get("selftext")
        removed = bool(raw.get("removed_by_category")) or body == "[removed]"
        deleted = body == "[deleted]"
        status: Status = "removed" if removed else "deleted" if deleted else "available"
        if status == "available" and not isinstance(title, str):
            raise KeyError("title")
        if not isinstance(permalink, str):
            raise TypeError("permalink")
        reference = source("post", permalink, now, status, subreddit=subreddit, post_id=post_id)
        text = body if isinstance(body, str) and status == "available" else None
        external = raw.get("url") if not raw.get("is_self", True) and status == "available" else None
        return Post(post_id=post_id, title=title if isinstance(title, str) else None,
                    author=raw.get("author") if raw.get("author") != "[deleted]" else None,
                    subreddit=subreddit, permalink=reference.permalink,
                    created_at=utc_timestamp(raw.get("created_utc")),
                    score=raw.get("score") if isinstance(raw.get("score"), int) else None,
                    comment_count=raw.get("num_comments") if isinstance(raw.get("num_comments"), int) else None,
                    flair=raw.get("link_flair_text"), text_preview=text[:300] if text else None,
                    selftext=text, external_url=external, edited=bool(raw.get("edited")) if "edited" in raw else None,
                    status=status, research_source=reference)
    except (KeyError, TypeError, ValueError) as error:
        raise RedditFailure("MALFORMED_POST") from error


def parse_comment(raw: Mapping[str, Any], now: str, post_id: str, subreddit: str) -> Comment:
    try:
        comment_id = validate_id(raw["id"], comment=True)
        parent_id = raw["parent_id"]
        if not isinstance(parent_id, str) or not re.fullmatch(r"t[13]_[a-z0-9]{1,20}", parent_id):
            raise ValueError("parent_id")
        if raw.get("link_id") != f"t3_{post_id}":
            raise ValueError("link_id")
        body = raw.get("body")
        status: Status = "removed" if body == "[removed]" else "deleted" if body == "[deleted]" else "collapsed" if raw.get("collapsed") else "available"
        if status in {"available", "collapsed"} and not isinstance(body, str):
            raise ValueError("body")
        permalink = raw["permalink"]
        reference = source("comment", permalink, now, status, subreddit=subreddit,
                           post_id=post_id, comment_id=comment_id)
        return Comment(comment_id=comment_id, parent_id=parent_id, post_id=post_id,
                       author=raw.get("author") if raw.get("author") != "[deleted]" else None,
                       text=body if status in {"available", "collapsed"} else None,
                       score=raw.get("score") if isinstance(raw.get("score"), int) else None,
                       created_at=utc_timestamp(raw.get("created_utc")), depth=0,
                       permalink=reference.permalink, status=status, research_source=reference)
    except (KeyError, TypeError, ValueError) as error:
        raise RedditFailure("MALFORMED_COMMENT") from error


def flatten_comments(items: list[Mapping[str, Any]], now: str, post_id: str,
                     subreddit: str) -> tuple[list[Comment], list[str]]:
    flat: list[Comment] = []
    unresolved_parent_ids: list[str] = []

    def visit(children: list[Mapping[str, Any]], level: int = 0) -> None:
        if level > 64 or len(flat) > 500:
            raise RedditFailure("COMMENT_TREE_TOO_LARGE")
        for item in children:
            if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
                raise RedditFailure("MALFORMED_COMMENT_TREE")
            if item.get("kind") == "more":
                parent_id = item["data"].get("parent_id")
                if not isinstance(parent_id, str) or not re.fullmatch(r"t[13]_[a-z0-9]{1,20}", parent_id):
                    raise RedditFailure("MALFORMED_COMMENT_TREE")
                unresolved_parent_ids.append(parent_id)
                continue
            if item.get("kind") != "t1":
                raise RedditFailure("MALFORMED_COMMENT_TREE")
            data = item["data"]
            flat.append(parse_comment(data, now, post_id, subreddit))
            if len(flat) > 500:
                raise RedditFailure("COMMENT_TREE_TOO_LARGE")
            replies = data.get("replies")
            if isinstance(replies, dict):
                nested = replies.get("data", {}).get("children")
                if not isinstance(nested, list):
                    raise RedditFailure("MALFORMED_COMMENT_TREE")
                visit(nested, level + 1)
            elif replies not in (None, ""):
                raise RedditFailure("MALFORMED_COMMENT_TREE")

    visit(items)
    return flat, unresolved_parent_ids


def build_comment_tree(flat: list[Comment], post_id: str, limit: int, max_depth: int,
                       *, unresolved_parent_ids: list[str] | None = None) -> tuple[list[Comment], Pagination]:
    if not 1 <= limit <= 100 or not 1 <= max_depth <= 20:
        raise ValueError("Invalid comment limit or depth")
    nodes = {c.comment_id: c.model_copy(deep=True) for c in flat if c.comment_id}
    if len(nodes) != len(flat):
        raise RedditFailure("DUPLICATE_COMMENT_ID")
    roots: list[Comment] = []
    truncated_depth = False
    included = 0
    visiting: set[str] = set()

    children: dict[str, list[Comment]] = {}
    for original in flat:
        node = nodes[original.comment_id]
        children.setdefault(node.parent_id, []).append(node)
        if node.parent_id.startswith("t1_") and node.parent_id[3:] not in nodes:
            node.relation_status = "missing_parent"

    def add(node: Comment, depth: int) -> None:
        nonlocal included, truncated_depth
        if included >= limit:
            return
        if depth >= max_depth:
            truncated_depth = True
            return
        if node.comment_id in visiting:
            raise RedditFailure("COMMENT_CYCLE")
        visiting.add(node.comment_id or "")
        node.depth = depth
        included += 1
        for child in children.get(f"t1_{node.comment_id}", []):
            if included >= limit:
                break
            if depth + 1 >= max_depth:
                truncated_depth = True
                break
            node.replies.append(child)
            add(child, depth + 1)
        visiting.remove(node.comment_id or "")

    for original in flat:
        node = nodes[original.comment_id]
        if node.parent_id == f"t3_{post_id}" or node.relation_status == "missing_parent":
            if included >= limit:
                break
            roots.append(node)
            add(node, 0)
    if flat and not roots:
        raise RedditFailure("COMMENT_CYCLE_OR_BAD_ROOT")
    truncated_limit = included >= limit and included < len(flat)
    unresolved_parent_ids = unresolved_parent_ids or []
    pagination = Pagination(complete=not (unresolved_parent_ids or truncated_limit or truncated_depth or any(
        c.relation_status == "missing_parent" for c in nodes.values())),
        more_available=bool(unresolved_parent_ids), unresolved_parent_ids=unresolved_parent_ids,
        truncated_by_limit=truncated_limit,
        truncated_by_depth=truncated_depth)
    return roots, pagination


def parse_response(status: int, payload: str) -> Any:
    if status in {401, 403, 404, 429}:
        raise RedditFailure({401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED"}[status], status)
    if not 200 <= status < 300:
        raise RedditFailure("HTTP_ERROR", status)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RedditFailure("MALFORMED_JSON", status) from error
    if not isinstance(value, (dict, list)):
        raise RedditFailure("MALFORMED_RESPONSE", status)
    return value


class RateLimitController:
    """One conservative request gate; future live client must call acquire and observe."""

    def __init__(self, minimum_interval: float = 2.0, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if minimum_interval < 0:
            raise ValueError("minimum_interval must be nonnegative")
        self.minimum_interval = minimum_interval
        self.clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.used: float | None = None
        self.remaining: float | None = None
        self.reset_seconds: float | None = None

    @asynccontextmanager
    async def request_slot(self):
        """Hold this slot through the request and observe its response before exit."""
        async with self._lock:
            delay = self._next_at - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self._next_at = self.clock() + self.minimum_interval
            yield

    def observe(self, status: int, headers: Mapping[str, str]) -> None:
        headers = {k.lower(): v for k, v in headers.items()}
        self.used = self.remaining = self.reset_seconds = None
        for key, attribute in (("x-ratelimit-used", "used"),
                               ("x-ratelimit-remaining", "remaining"),
                               ("x-ratelimit-reset", "reset_seconds")):
            try:
                value = float(headers[key])
                setattr(self, attribute, max(0.0, value))
            except (KeyError, ValueError):
                pass
        if self.remaining is not None and self.remaining <= 0 and self.reset_seconds is not None:
            self._next_at = max(self._next_at, self.clock() + self.reset_seconds)
        if status == 429:
            retry = headers.get("retry-after", "")
            try:
                seconds = max(0.0, float(retry))
            except ValueError:
                try:
                    seconds = max(0.0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    seconds = 60.0
            self._next_at = max(self._next_at, self.clock() + seconds)
