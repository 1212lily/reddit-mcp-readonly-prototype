"""MCP schema and approval gate. No live Reddit client is implemented."""
from __future__ import annotations

import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .core import RedditFailure, post_id_from_reference, username_from_reference, validate_id, validate_subreddit


mcp = FastMCP("reddit-read-offline", instructions=(
    "Reddit Data API approval is pending. check_access reports the gate. "
    "All content tools are blocked until approved live integration is implemented. "
    "Never treat fixture tests as real Reddit evidence. No account actions are available."
))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def approval_gate() -> None:
    raise RedditFailure("REDDIT_API_APPROVAL_PENDING")


@mcp.tool(annotations=READ)
async def check_access() -> dict[str, Any]:
    """Report access readiness without returning any credentials."""
    return {"ready": False, "auth_mode": "none", "user_authorized": False,
            "username": None, "scopes": [], "access_status": "pending",
            "real_api_integration": "blocked"}


@mcp.tool(annotations=READ)
async def browse_subreddit(
    subreddit: Annotated[str, Field(min_length=2, max_length=21, pattern=r"^[A-Za-z0-9_]{2,21}$")],
    sort: Literal["hot", "new", "top", "rising"] = "hot",
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] = "all",
    limit: Annotated[int, Field(ge=1, le=50)] = 25,
) -> dict[str, Any]:
    """Browse bounded public posts after Reddit approves access; blocked now."""
    validate_subreddit(subreddit)
    approval_gate()


@mcp.tool(annotations=READ)
async def search_posts(
    query: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[\s\S]*\S[\s\S]*$")],
    subreddit: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9_]{2,21}$")] = None,
    sort: Literal["relevance", "hot", "top", "new", "comments"] = "relevance",
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] = "all",
    limit: Annotated[int, Field(ge=1, le=50)] = 25,
) -> dict[str, Any]:
    """Search public posts after Reddit approves access; blocked now."""
    if subreddit is not None:
        validate_subreddit(subreddit)
    approval_gate()


@mcp.tool(annotations=READ)
async def get_post(
    post_id: Annotated[str | None, Field(max_length=23)] = None,
    post_url: Annotated[str | None, Field(max_length=500)] = None,
) -> dict[str, Any]:
    """Read one post. Supply exactly one of post_id or post_url; blocked now."""
    post_id_from_reference(post_id, post_url)
    approval_gate()


@mcp.tool(annotations=READ)
async def get_comments(
    post_id: Annotated[str | None, Field(max_length=23)] = None,
    post_url: Annotated[str | None, Field(max_length=500)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    depth: Annotated[int, Field(ge=1, le=20)] = 8,
    sort: Literal["best", "top", "new", "controversial", "old"] = "best",
) -> dict[str, Any]:
    """Read a bounded comment tree after Reddit approves access; blocked now."""
    post_id_from_reference(post_id, post_url)
    approval_gate()


@mcp.tool(annotations=READ)
async def get_thread_context(
    comment_id: Annotated[str, Field(min_length=1, max_length=23, pattern=r"^(?:t1_)?[a-z0-9]{1,20}$")],
) -> dict[str, Any]:
    """Read parent chain and bounded replies after Reddit approves access; blocked now."""
    validate_id(comment_id, comment=True)
    approval_gate()


@mcp.tool(annotations=READ)
async def get_subreddit(
    name: Annotated[str, Field(min_length=2, max_length=21, pattern=r"^[A-Za-z0-9_]{2,21}$")],
) -> dict[str, Any]:
    """Read public subreddit information after Reddit approves access; blocked now."""
    validate_subreddit(name)
    approval_gate()


@mcp.tool(annotations=READ)
async def get_profile(
    username: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9_-]{3,20}$")] = None,
    profile_url: Annotated[str | None, Field(max_length=500)] = None,
) -> dict[str, Any]:
    """Read public profile fields only after Reddit approves access; blocked now."""
    username_from_reference(username, profile_url)
    approval_gate()


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport != "stdio":
        raise SystemExit("Only local stdio is available before Reddit approval")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
