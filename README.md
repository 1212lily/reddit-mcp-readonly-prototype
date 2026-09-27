# Reddit MCP — read-only offline prototype

**Status: Offline scaffold only.**

- No live Reddit API access is implemented or enabled.
- Reddit API approval is pending.
- No browser scraping or anonymous fallback is used.
- No write operations are implemented.
- All current tests use local fixtures/mocks only.

```text
OFFLINE_SCAFFOLD = PASS
OFFLINE_TESTS = 19 PASSED / 0 FAILED
REDDIT_API_APPROVAL = PENDING
REAL_API_INTEGRATION = BLOCKED
```

## Overview

This local FastMCP server prototype defines bounded, read-only tools for public Reddit posts, comment trees, subreddit information, and basic public profile fields. It is fully offline. A real client may use Reddit's approved Data API and OAuth only after Reddit approves this use case and the owner authorizes further work. No Reddit App has been created, no access request has been submitted, and no real Reddit login or OAuth has occurred.

## Current Tool Surface

| Tool | Input | Intended approved result |
|---|---|---|
| `check_access` | none | Approval, authorization mode, scopes, readiness; never credentials |
| `browse_subreddit` | `subreddit`, `sort`, `time_filter`, `limit <= 50` | Bounded public post list |
| `search_posts` | `query`, optional `subreddit`, `sort`, `time_filter`, `limit <= 50` | Bounded public post search |
| `get_post` | exactly one of `post_id`, `post_url` | One post, including deleted/removed state |
| `get_comments` | one post reference, `limit <= 100`, `depth <= 20`, `sort` | Parent-linked tree plus completeness flags |
| `get_thread_context` | `comment_id` | Post, ancestors, target, bounded direct replies, completeness |
| `get_subreddit` | `name` | Public community details and rules, if available |
| `get_profile` | exactly one of `username`, `profile_url` | Basic public profile fields only |

**All content tools are blocked while access is not approved.** `check_access` reports `ready=false`, `auth_mode="none"`, and `access_status="pending"`. Each of the other seven tools raises `REDDIT_API_APPROVAL_PENDING` after input validation. The offline fixture service is separate from the MCP server and cannot enable live access.

## Safety Boundaries

There is no posting, commenting, voting, saving, messaging, joining, following, user profiling, browser scraping, deleted-content recovery, bulk collection, model training, or ad targeting. The public profile lookup returns only a username, profile URL, account creation time, total karma, public biography, and source metadata from fabricated fixtures. No account history aggregation or inferred traits are provided.

## Architecture

```text
FastMCP → V1 tool schemas → unconditional approval gate
                               ↓ only after approval and new authorization
                      future approved Reddit API client → structured source metadata
```

`src/reddit_mcp/server.py` defines the tool surface and gate. `models.py` and `core.py` define public content shapes, input checks, parsing, and an unused future client interface. `offline.py` reads fabricated data from `tests/fixtures/sample.json`; it is not connected to the MCP tools.

## Comment Tree Support

Offline comment parsing preserves `comment_id`, `parent_id`, `post_id`, author, public text, score, creation time, `depth`, permalink, and status. It keeps parent and sibling relationships. Pagination reports `complete`, `more_available`, unresolved parents, and truncation flags; thread context separately reports `context_complete`. Deleted or removed text is not reconstructed.

## Research Source Metadata

Appropriate records include `research_source` with platform, source type, subreddit, post/comment IDs, permalink, retrieval time, status, and `data_origin`. For example, the fabricated fixture response is shaped as:

```json
{
  "fixture_only": true,
  "research_source": {
    "source_platform": "reddit",
    "source_type": "comment",
    "subreddit": "LocalLLaMA",
    "post_id": "abc123",
    "comment_id": "c1",
    "permalink": "https://www.reddit.com/r/LocalLLaMA/comments/abc123/example/c1/",
    "retrieved_at": "2026-09-27T00:00:00+00:00",
    "status": "available",
    "data_origin": "fixture"
  }
}
```

`fixture_only` appears only in offline test data, at the response level. The example is illustrative and not a real Reddit record.

## Authentication

Live OAuth is not configured. No real Reddit credentials are required or used by the offline scaffold. `.env.example` contains names only; `.gitignore` excludes local secret files. Approved credentials would be configured only in a separately authorized integration.

## Testing

The current offline suite passes **19 tests, 0 failed**. It checks parsing, comment-tree states, fixture output, the eight-tool MCP schema, access status, and the approval gate for every content tool. With Python 3.12 and the declared dependencies available, set `PYTHONPATH` to this repository's `src` directory and run:

```text
python -m unittest discover -s tests -v
```

The tests use local files and a local stdio MCP subprocess. They make no Reddit API requests. `python -m reddit_mcp.server` starts only a local stdio server whose content tools remain blocked.

## API Approval

Live Reddit integration is intentionally blocked until Reddit grants Data API access for this use case. [ACCESS_APPLICATION.md](ACCESS_APPLICATION.md) is an unsubmitted draft for the owner's review; [APPLICATION_CHECKLIST.md](APPLICATION_CHECKLIST.md) records the manual submission steps. Neither document contains an application reference.

## License and provenance

No license file has been added. The owner must decide whether to grant reuse rights before publication. The prototype source and synthetic fixtures were written for this project; no third-party source code is vendored. `mcp`, `pydantic`, and `setuptools` are declared dependencies rather than copied code.

## Public release checklist

- [ ] Review the application wording and confirm the personal, non-commercial use statement.
- [ ] Recheck for credentials, personal identifiers, local paths, and generated files before upload.
- [ ] Decide whether to add a license; no license is currently granted by this repository.
- [ ] Upload only the source, tests, fixtures, and documentation listed in this repository; exclude ignored and private files.
- [ ] Create or publish the repository only after the owner decides to do so.

## NEXT STAGE AFTER APPROVAL

1. Confirm approved access scope
2. Configure approved OAuth credentials
3. Implement real Reddit client
4. Implement rate-limit header handling
5. Perform one low-frequency real read
6. Verify post parsing
7. Verify comment tree parsing
8. Verify deletion/removed states
9. Connect MCP to ChatGPT
10. Perform end-to-end read-only acceptance

Implementation remains frozen until Reddit approval and new owner authorization.
