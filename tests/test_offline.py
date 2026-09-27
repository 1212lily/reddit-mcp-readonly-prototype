from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reddit_mcp.core import (RateLimitController, RedditFailure, build_comment_tree,
                             flatten_comments, parse_post, parse_response,
                             post_id_from_reference, username_from_reference, validate_subreddit)
from reddit_mcp.offline import MockRedditClient, OfflineService


FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"
NOW = "2026-09-27T00:00:00+00:00"


class ParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_post_types_and_deleted_content(self):
        posts = [parse_post(raw, NOW) for raw in self.fixture["posts"]]
        self.assertEqual([p.status for p in posts], ["available", "available", "deleted", "removed"])
        self.assertEqual(posts[0].selftext, "A fictional example about memory.")
        self.assertEqual(posts[1].external_url, "https://example.invalid/guide")
        self.assertIsNone(posts[2].selftext)
        self.assertIsNone(posts[3].selftext)
        self.assertIsNone(posts[2].author)

    def test_missing_post_field_fails(self):
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_POST"):
            parse_post({"id": "abc123", "subreddit": "Roleplay", "permalink": "/r/Roleplay/comments/abc123/x/"}, NOW)

    def test_url_and_subreddit_validation(self):
        self.assertEqual(post_id_from_reference(None, "https://www.reddit.com/r/Roleplay/comments/abc123/title/"), "abc123")
        self.assertEqual(post_id_from_reference("t3_abc123", None), "abc123")
        for url in ("http://reddit.com/r/Roleplay/comments/abc123/x/",
                    "https://reddit.com.evil.invalid/r/Roleplay/comments/abc123/x/",
                    "https://reddit.com/user/person"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                post_id_from_reference(None, url)
        with self.assertRaises(ValueError):
            post_id_from_reference("abc123", "https://redd.it/abc123")
        for name in ("r/foo", "a", "bad space", "foo.bar"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_subreddit(name)
        self.assertEqual(username_from_reference(None, "https://www.reddit.com/user/sample_user/"), "sample_user")
        for url in ("https://reddit.com:8443/user/sample_user/", "https://reddit.com.evil.invalid/user/sample_user/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                username_from_reference(None, url)

    def test_nested_siblings_and_source_metadata(self):
        flat, unresolved = flatten_comments(self.fixture["comments"]["abc123"], NOW, "abc123", "LocalLLaMA")
        roots, page = build_comment_tree(flat, "abc123", 100, 8, unresolved_parent_ids=unresolved)
        self.assertEqual([c.comment_id for c in roots], ["c1", "c5"])
        self.assertEqual([c.comment_id for c in roots[0].replies], ["c2", "c4"])
        self.assertEqual(roots[0].replies[0].replies[0].comment_id, "c3")
        self.assertEqual(roots[0].replies[0].replies[0].depth, 2)
        self.assertEqual(roots[0].replies[0].replies[0].author, "carol")
        self.assertEqual(roots[0].replies[0].parent_id, "t1_c1")
        self.assertTrue(page.more_available)
        self.assertEqual(page.unresolved_parent_ids, ["t3_abc123"])
        self.assertFalse(page.complete)
        source = roots[0].replies[0].research_source
        self.assertEqual((source.post_id, source.comment_id, source.subreddit), ("abc123", "c2", "LocalLLaMA"))
        self.assertEqual(source.retrieved_at, NOW)
        self.assertEqual(source.status, "available")
        self.assertTrue(source.permalink.endswith("/c2/"))

    def test_deleted_parent_and_orphan(self):
        raw = self.fixture["comments"]["abc123"]
        flat, _ = flatten_comments(raw, NOW, "abc123", "LocalLLaMA")
        flat[0].text = None
        flat[0].status = "deleted"
        flat[0].research_source.status = "deleted"
        roots, _ = build_comment_tree(flat, "abc123", 100, 8)
        self.assertIsNone(roots[0].text)
        self.assertEqual(roots[0].replies[0].parent_id, "t1_c1")
        orphan = flat[1].model_copy(deep=True)
        orphan.parent_id = "t1_missing"
        roots, page = build_comment_tree([orphan], "abc123", 100, 8)
        self.assertEqual(roots[0].relation_status, "missing_parent")
        self.assertFalse(page.complete)

    def test_comment_deleted_removed_and_collapsed_status(self):
        base = dict(self.fixture["comments"]["abc123"][1]["data"])
        for body, collapsed, expected in (("[deleted]", False, "deleted"),
                                          ("[removed]", False, "removed"),
                                          ("Still visible", True, "collapsed")):
            with self.subTest(expected=expected):
                raw = dict(base, body=body, collapsed=collapsed)
                flat, _ = flatten_comments([{"kind": "t1", "data": raw}], NOW, "abc123", "LocalLLaMA")
                self.assertEqual(flat[0].status, expected)
                self.assertEqual(flat[0].research_source.status, expected)
                if expected in {"deleted", "removed"}:
                    self.assertIsNone(flat[0].text)
                else:
                    self.assertEqual(flat[0].text, "Still visible")

    def test_depth_and_limit_truncation(self):
        flat, _ = flatten_comments(self.fixture["comments"]["abc123"], NOW, "abc123", "LocalLLaMA")
        roots, page = build_comment_tree(flat, "abc123", 100, 2)
        self.assertTrue(page.truncated_by_depth)
        self.assertEqual(roots[0].replies[0].replies, [])
        roots, page = build_comment_tree(flat, "abc123", 2, 8)
        self.assertTrue(page.truncated_by_limit)
        self.assertEqual([c.comment_id for c in roots[0].replies], ["c2"])

    def test_malformed_comment_tree_fails(self):
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_COMMENT"):
            flatten_comments([{"kind": "t1", "data": {"id": "c1", "parent_id": "t3_wrong"}}], NOW, "abc123", "LocalLLaMA")
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_COMMENT_TREE"):
            base = dict(self.fixture["comments"]["abc123"][1]["data"])
            base["replies"] = {}
            flatten_comments([{"kind": "t1", "data": base}], NOW, "abc123", "LocalLLaMA")

    def test_http_and_json_failures_are_not_empty_results(self):
        for status, code in ((401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"), (429, "RATE_LIMITED")):
            with self.subTest(status=status), self.assertRaisesRegex(RedditFailure, code):
                parse_response(status, "[]")
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_JSON"):
            parse_response(200, "{")
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_RESPONSE"):
            parse_response(200, "null")
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_RESPONSE"):
            asyncio.run(OfflineService(MockRedditClient({}), now=NOW).search_posts("any"))


class OfflineServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = OfflineService(MockRedditClient.from_file(FIXTURE), now=NOW)

    async def test_browse_and_search_are_bounded_fixtures(self):
        browse = await self.service.browse_subreddit("LocalLLaMA", limit=1)
        self.assertTrue(browse["fixture_only"])
        self.assertEqual(len(browse["results"]), 1)
        self.assertTrue(browse["truncated_by_limit"])
        search = await self.service.search_posts("memory")
        self.assertEqual([p["post_id"] for p in search["results"]], ["abc123"])
        self.assertEqual(search["results"][0]["research_source"]["data_origin"], "fixture")
        self.assertEqual((await self.service.search_posts("不存在"))["results"], [])

    async def test_comment_context_and_public_profile(self):
        comments = await self.service.get_comments(post_id="abc123")
        self.assertEqual(comments["comments"][0]["replies"][0]["comment_id"], "c2")
        context = await self.service.get_thread_context("c3")
        self.assertEqual([c["comment_id"] for c in context["ancestors"]], ["c1", "c2"])
        self.assertEqual(context["target_comment"]["parent_id"], "t1_c2")
        profile = await self.service.get_profile(username="sample_user")
        self.assertEqual(profile["public_bio"], "Fixture biography")
        self.assertNotIn("interests", profile)
        self.assertNotIn("personality", profile)

    async def test_absent_and_partial_fixture_fail(self):
        with self.assertRaisesRegex(RedditFailure, "NOT_FOUND"):
            await self.service.get_post(post_id="missing")
        broken = MockRedditClient({"posts": [json.loads(FIXTURE.read_text(encoding="utf-8"))["posts"][0]],
                                   "comments": {"abc123": {"children": []}}})
        with self.assertRaisesRegex(RedditFailure, "MALFORMED_RESPONSE"):
            await OfflineService(broken, now=NOW).get_comments(post_id="abc123")

    async def test_timeout_is_a_failure_not_zero_results(self):
        class TimeoutClient:
            async def read(self, route, params=None):
                raise RedditFailure("TIMEOUT")

        with self.assertRaisesRegex(RedditFailure, "TIMEOUT"):
            await OfflineService(TimeoutClient(), now=NOW).search_posts("memory")

    async def test_post_subreddit_and_profile_sources(self):
        post = (await self.service.get_post(post_id="abc123"))["post"]
        community = await self.service.get_subreddit("LocalLLaMA")
        profile = await self.service.get_profile(profile_url="https://www.reddit.com/user/sample_user/")
        self.assertEqual((post["research_source"]["source_type"],
                          community["research_source"]["source_type"],
                          profile["research_source"]["source_type"]), ("post", "subreddit", "profile"))
        self.assertEqual(post["research_source"]["post_id"], "abc123")
        self.assertEqual(profile["research_source"]["retrieved_at"], NOW)

    async def test_schema_like_limits_and_enums(self):
        for kwargs in ({"limit": 51}, {"sort": "unsupported"}, {"time_filter": "decade"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.service.browse_subreddit("LocalLLaMA", **kwargs)
        with self.assertRaises(ValueError):
            await self.service.get_comments(post_id="abc123", depth=21)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_pacing_headers_and_retry_after(self):
        current = [100.0]
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            current[0] += seconds

        gate = RateLimitController(2, clock=lambda: current[0], sleep=sleep)
        async with gate.request_slot():
            pass
        async with gate.request_slot():
            pass
        self.assertEqual(sleeps, [2])
        async with gate.request_slot():
            gate.observe(200, {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "10"})
        self.assertEqual((gate.remaining, gate.reset_seconds), (0, 10))
        async with gate.request_slot():
            pass
        self.assertEqual(sleeps[-1], 10)
        async with gate.request_slot():
            gate.observe(429, {"Retry-After": "7"})
        async with gate.request_slot():
            pass
        self.assertEqual(sleeps[-1], 7)
        gate.observe(200, {"X-Ratelimit-Used": "3", "X-Ratelimit-Remaining": "97"})
        self.assertEqual((gate.used, gate.remaining, gate.reset_seconds), (3, 97, None))

    async def test_global_serialization(self):
        current = [100.0]

        async def sleep(seconds):
            current[0] += seconds

        gate = RateLimitController(3, clock=lambda: current[0], sleep=sleep)
        active = [0]
        peak = [0]

        async def request():
            async with gate.request_slot():
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                await asyncio.sleep(0)
                active[0] -= 1

        await asyncio.gather(*(request() for _ in range(3)))
        self.assertEqual(current[0], 106)
        self.assertEqual(peak[0], 1)


class McpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_handshake_schema_and_approval_gate(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        server = StdioServerParameters(command=sys.executable, args=["-m", "reddit_mcp.server"], env=env)
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual(set(tools), {"check_access", "browse_subreddit", "search_posts", "get_post",
                                              "get_comments", "get_thread_context", "get_subreddit", "get_profile"})
                self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools.values()))
                self.assertEqual(tools["browse_subreddit"].inputSchema["properties"]["limit"]["maximum"], 50)
                self.assertEqual(tools["get_comments"].inputSchema["properties"]["depth"]["maximum"], 20)
                access = await session.call_tool("check_access")
                self.assertFalse(access.structuredContent["ready"])
                self.assertEqual(access.structuredContent["access_status"], "pending")
                for name, args in {
                    "browse_subreddit": {"subreddit": "LocalLLaMA"},
                    "search_posts": {"query": "memory"},
                    "get_post": {"post_id": "abc123"},
                    "get_comments": {"post_id": "abc123"},
                    "get_thread_context": {"comment_id": "c1"},
                    "get_subreddit": {"name": "LocalLLaMA"},
                    "get_profile": {"username": "sample_user"},
                }.items():
                    with self.subTest(tool=name):
                        blocked = await session.call_tool(name, args)
                        self.assertTrue(blocked.isError)
                        self.assertIn("REDDIT_API_APPROVAL_PENDING", str(blocked.content))


class PublicReleaseTests(unittest.TestCase):
    def test_public_files_exclude_local_secrets_and_paths(self):
        root = Path(__file__).resolve().parents[1]
        for name in (".env", "credentials.json", "secrets.json"):
            self.assertFalse((root / name).exists(), name)
        self.assertNotRegex((root / "README.md").read_text(encoding="utf-8"), r"(?i)\b[a-z]:[\\/]")
        ignore = set((root / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertTrue({".env", ".env.*", "!.env.example", "__pycache__/", ".pytest_cache/",
                         ".venv/", "venv/", "*.pyc", "*.pyo", "*.log", ".DS_Store",
                         "credentials.json", "secrets.json", "*.pem", "*.key", "*.p12"} <= ignore)


if __name__ == "__main__":
    unittest.main()
