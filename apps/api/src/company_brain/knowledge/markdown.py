import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import yaml


@dataclass(frozen=True)
class ParsedMarkdown:
    frontmatter: dict[str, Any]
    body: str
    plain_text: str
    tags: list[str]
    links: list[str]


@dataclass(frozen=True)
class MarkdownChunk:
    index: int
    heading_path: list[str]
    text: str
    start_offset: int
    end_offset: int
    content_hash: str


def parse_markdown(markdown: str) -> ParsedMarkdown:
    frontmatter: dict[str, Any] = {}
    body = markdown
    if markdown.startswith("---\n"):
        closing = markdown.find("\n---\n", 4)
        if closing == -1:
            raise ValueError("Frontmatter closing delimiter is missing")
        raw = markdown[4:closing]
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as error:
            raise ValueError("Frontmatter is not valid YAML") from error
        if not isinstance(loaded, dict):
            raise ValueError("Frontmatter must be an object")
        try:
            json.dumps(loaded, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Frontmatter values must be JSON-compatible") from error
        frontmatter = loaded
        body = markdown[closing + 5 :]
    raw_tags = frontmatter.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
        raise ValueError("Frontmatter tags must be a list of strings")
    tags = list(dict.fromkeys(tag.strip().lower() for tag in raw_tags if tag.strip()))
    raw_links = [
        match.strip() for match in re.findall(r"\[\[([^]|]+)(?:\|[^]]+)?\]\]", body)
    ]
    links_by_normalized_target: dict[str, str] = {}
    for link in raw_links:
        normalized = " ".join(link.casefold().split())
        if normalized:
            links_by_normalized_target.setdefault(normalized, link)
    links = list(links_by_normalized_target.values())
    plain_text = re.sub(r"\[\[([^]|]+)(?:\|[^]]+)?\]\]", r"\1", body)
    plain_text = re.sub(r"[#*_`>]", "", plain_text)
    plain_text = " ".join(plain_text.split())
    return ParsedMarkdown(frontmatter, body, plain_text, tags, links)


def chunk_markdown(markdown: str) -> list[MarkdownChunk]:
    body_start = 0
    if markdown.startswith("---\n"):
        closing = markdown.find("\n---\n", 4)
        if closing == -1:
            raise ValueError("Frontmatter closing delimiter is missing")
        body_start = closing + 5
    lines = markdown[body_start:].splitlines(keepends=True)
    heading_path: list[str] = []
    chunks: list[MarkdownChunk] = []
    buffer: list[str] = []
    start = body_start
    offset = body_start

    def flush(end: int) -> None:
        raw = "".join(buffer)
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        text = raw[leading:trailing]
        if text:
            chunk_start = start + leading
            chunk_end = start + trailing
            chunks.append(
                MarkdownChunk(
                    index=len(chunks),
                    heading_path=list(heading_path),
                    text=text,
                    start_offset=chunk_start,
                    end_offset=chunk_end,
                    content_hash=sha256(text.encode()).hexdigest(),
                )
            )
        buffer.clear()

    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush(offset)
            level = len(heading.group(1))
            heading_path[:] = heading_path[: level - 1]
            heading_path.append(heading.group(2))
            start = offset + len(line)
        else:
            if not buffer:
                start = offset
            buffer.append(line)
        offset += len(line)
    flush(offset)
    return chunks
