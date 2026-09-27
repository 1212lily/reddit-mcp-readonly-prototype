from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Status = Literal["available", "deleted", "removed", "collapsed", "unresolved", "unavailable"]
SourceType = Literal["post", "comment", "subreddit", "profile"]


class ResearchSource(BaseModel):
    source_platform: Literal["reddit"] = "reddit"
    source_type: SourceType
    subreddit: str | None = None
    post_id: str | None = None
    comment_id: str | None = None
    permalink: str
    retrieved_at: str
    status: Status
    data_origin: Literal["fixture", "api"] = "fixture"


class Post(BaseModel):
    post_id: str
    title: str | None
    author: str | None
    subreddit: str
    permalink: str
    created_at: str | None
    score: int | None
    comment_count: int | None
    flair: str | None
    text_preview: str | None
    selftext: str | None
    external_url: str | None
    edited: bool | None
    status: Status
    research_source: ResearchSource


class Comment(BaseModel):
    comment_id: str | None
    parent_id: str
    post_id: str
    author: str | None
    text: str | None
    score: int | None
    created_at: str | None
    depth: int = Field(ge=0)
    permalink: str
    status: Status
    relation_status: Literal["linked", "missing_parent"] = "linked"
    replies: list[Comment] = Field(default_factory=list)
    research_source: ResearchSource


class Pagination(BaseModel):
    complete: bool
    more_available: bool
    unresolved_parent_ids: list[str] = Field(default_factory=list)
    truncated_by_limit: bool
    truncated_by_depth: bool


Comment.model_rebuild()
