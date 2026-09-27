"""Fixture-backed read paths for tests. This module cannot contact Reddit."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .core import (RedditFailure, build_comment_tree, flatten_comments,
                   parse_post, post_id_from_reference, source, validate_id,
                   validate_subreddit, username_from_reference, utc_timestamp)


class MockRedditClient:
    def __init__(self, fixture: Mapping[str, Any]):
        self.fixture = fixture

    @classmethod
    def from_file(cls, path: Path) -> MockRedditClient:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RedditFailure("MALFORMED_FIXTURE") from error
        if not isinstance(data, dict):
            raise RedditFailure("MALFORMED_FIXTURE")
        return cls(data)

    async def read(self, route: str, params: Mapping[str, str | int] | None = None) -> Any:
        if route == "posts":
            if "posts" not in self.fixture:
                raise RedditFailure("MALFORMED_RESPONSE")
            return self.fixture["posts"]
        if route.startswith("comments/"):
            post_id = validate_id(route.removeprefix("comments/"))
            return self.fixture.get("comments", {}).get(post_id)
        if route == "subreddits":
            return self.fixture.get("subreddits", {})
        if route == "profiles":
            return self.fixture.get("profiles", {})
        raise RedditFailure("UNKNOWN_MOCK_ROUTE")


class OfflineService:
    def __init__(self, client: MockRedditClient, *, now: str | None = None):
        self.client = client
        self.now = now or datetime.now(timezone.utc).isoformat()

    async def _posts(self):
        raw = await self.client.read("posts")
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise RedditFailure("MALFORMED_RESPONSE")
        return [parse_post(item, self.now) for item in raw]

    async def browse_subreddit(self, subreddit: str, sort: str = "hot", time_filter: str = "all",
                              limit: int = 25) -> dict[str, Any]:
        validate_subreddit(subreddit)
        if sort not in {"hot", "new", "top", "rising"} or time_filter not in {
            "hour", "day", "week", "month", "year", "all"
        } or not 1 <= limit <= 50:
            raise ValueError("Invalid browse parameters")
        posts = [p for p in await self._posts() if p.subreddit.casefold() == subreddit.casefold()]
        posts = self._filter_time(posts, time_filter)
        if sort == "new":
            posts.sort(key=lambda p: p.created_at or "", reverse=True)
        elif sort == "top":
            posts.sort(key=lambda p: p.score or 0, reverse=True)
        return {"fixture_only": True, "subreddit": subreddit, "results": [p.model_dump() for p in posts[:limit]],
                "truncated_by_limit": len(posts) > limit}

    async def search_posts(self, query: str, subreddit: str | None = None, sort: str = "relevance",
                           time_filter: str = "all", limit: int = 25) -> dict[str, Any]:
        if not query.strip() or len(query) > 512 or not 1 <= limit <= 50 or sort not in {
            "relevance", "hot", "top", "new", "comments"
        } or time_filter not in {"hour", "day", "week", "month", "year", "all"}:
            raise ValueError("Invalid search parameters")
        if subreddit is not None:
            validate_subreddit(subreddit)
        posts = [p for p in await self._posts() if query.casefold() in (
            (p.title or "") + " " + (p.selftext or "")
        ).casefold() and (subreddit is None or p.subreddit.casefold() == subreddit.casefold())]
        posts = self._filter_time(posts, time_filter)
        if sort == "new":
            posts.sort(key=lambda p: p.created_at or "", reverse=True)
        elif sort == "top":
            posts.sort(key=lambda p: p.score or 0, reverse=True)
        elif sort == "comments":
            posts.sort(key=lambda p: p.comment_count or 0, reverse=True)
        return {"fixture_only": True, "query": query, "results": [p.model_dump() for p in posts[:limit]],
                "truncated_by_limit": len(posts) > limit}

    def _filter_time(self, posts, time_filter: str):
        if time_filter == "all":
            return posts
        seconds = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}[time_filter]
        now = datetime.fromisoformat(self.now)
        return [p for p in posts if p.created_at and (
            now - datetime.fromisoformat(p.created_at)).total_seconds() <= seconds]

    async def get_post(self, post_id: str | None = None, post_url: str | None = None) -> dict[str, Any]:
        target = post_id_from_reference(post_id, post_url)
        for post in await self._posts():
            if post.post_id == target:
                return {"fixture_only": True, "post": post.model_dump()}
        raise RedditFailure("NOT_FOUND", 404)

    async def _flat_comments(self, post_id: str):
        post = (await self.get_post(post_id=post_id))["post"]
        raw = await self.client.read(f"comments/{post_id}")
        if raw is None:
            raise RedditFailure("NOT_FOUND", 404)
        if not isinstance(raw, list):
            raise RedditFailure("MALFORMED_RESPONSE")
        flat, unresolved = flatten_comments(raw, self.now, post_id, post["subreddit"])
        return post, flat, unresolved

    async def get_comments(self, post_id: str | None = None, post_url: str | None = None,
                           limit: int = 50, depth: int = 8, sort: str = "best") -> dict[str, Any]:
        target = post_id_from_reference(post_id, post_url)
        if sort not in {"best", "top", "new", "controversial", "old"}:
            raise ValueError("Invalid comment sort")
        post, flat, unresolved = await self._flat_comments(target)
        roots, pagination = build_comment_tree(flat, target, limit, depth, unresolved_parent_ids=unresolved)
        return {"fixture_only": True, "post_id": target, "comments": [c.model_dump() for c in roots],
                "pagination": pagination.model_dump(), "post_source": post["research_source"]}

    async def get_thread_context(self, comment_id: str) -> dict[str, Any]:
        target_id = validate_id(comment_id, comment=True)
        # ponytail: scan tiny offline fixtures; an approved API client should resolve a comment to its post directly.
        posts = await self._posts()
        for post in posts:
            raw = await self.client.read(f"comments/{post.post_id}")
            if not isinstance(raw, list):
                continue
            flat, unresolved = flatten_comments(raw, self.now, post.post_id, post.subreddit)
            by_id = {c.comment_id: c for c in flat}
            target = by_id.get(target_id)
            if target is None:
                continue
            ancestors = []
            parent_id = target.parent_id
            complete = not unresolved
            seen = {target_id}
            while parent_id.startswith("t1_"):
                ancestor_id = parent_id[3:]
                if ancestor_id in seen or len(ancestors) >= 8:
                    complete = False
                    break
                seen.add(ancestor_id)
                parent = by_id.get(ancestor_id)
                if parent is None:
                    complete = False
                    break
                ancestors.append(parent.model_copy(deep=True))
                parent_id = parent.parent_id
            ancestors.reverse()
            for index, ancestor in enumerate(ancestors):
                ancestor.depth = index
            target = target.model_copy(deep=True)
            target.depth = len(ancestors)
            replies = [c.model_copy(deep=True) for c in flat if c.parent_id == f"t1_{target_id}"][:20]
            for reply in replies:
                reply.depth = target.depth + 1
            if len([c for c in flat if c.parent_id == f"t1_{target_id}"]) > len(replies):
                complete = False
            return {"fixture_only": True, "post": post.model_dump(),
                    "ancestors": [c.model_dump() for c in ancestors],
                    "target_comment": target.model_dump(),
                    "direct_or_bounded_replies": [c.model_dump() for c in replies],
                    "context_complete": complete}
        raise RedditFailure("NOT_FOUND", 404)

    async def get_subreddit(self, name: str) -> dict[str, Any]:
        validate_subreddit(name)
        data = await self.client.read("subreddits")
        if not isinstance(data, dict) or name not in data or not isinstance(data[name], dict):
            raise RedditFailure("NOT_FOUND", 404)
        raw = data[name]
        reference = source("subreddit", f"/r/{name}/", self.now, "available", subreddit=name)
        return {"fixture_only": True, "name": name, "title": raw.get("title"),
                "description": raw.get("description"), "rules": raw.get("rules"),
                "subscriber_count": raw.get("subscribers"), "url": reference.permalink,
                "research_source": reference.model_dump()}

    async def get_profile(self, username: str | None = None, profile_url: str | None = None) -> dict[str, Any]:
        username = username_from_reference(username, profile_url)
        data = await self.client.read("profiles")
        if not isinstance(data, dict) or username not in data or not isinstance(data[username], dict):
            raise RedditFailure("NOT_FOUND", 404)
        raw = data[username]
        reference = source("profile", f"/user/{username}/", self.now, "available")
        return {"fixture_only": True, "username": username, "profile_url": reference.permalink,
                "created_at": utc_timestamp(raw.get("created_utc")),
                "karma": raw.get("total_karma") if isinstance(raw.get("total_karma"), int) else None,
                "public_bio": raw.get("public_description"), "research_source": reference.model_dump()}
