"""Proxy routes - /v1/messages, /v1/models, /health."""

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from anything_client import SUPPORTED_MODELS, parse_response
from config import settings
from database.connection import execute, fetchone
from services.account_pool import account_pool
from services.pricing import DEFAULT_MODEL
from services.runtime_state import (
    get_session_documents,
    get_session_working_set,
    set_session_documents,
    set_session_working_set,
)
from services.token_counter import UsageTracker, count_tokens

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_UPSTREAM_MESSAGE_CHARS = 200000


# ─── Request Models ────────────────────────────────────────────────────


class ThinkingConfig(BaseModel):
    type: str = "enabled"
    budget_tokens: Optional[int] = None


class MessagesRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list
    max_tokens: int = 4096
    stream: bool = False
    system: Optional[str | list] = None
    temperature: Optional[float] = None
    thinking: Optional[ThinkingConfig] = None
    metadata: Optional[dict] = None
    tools: Optional[list] = None
    tool_choice: Optional[dict] = None


# ─── Auth ──────────────────────────────────────────────────────────────


def verify_api_key(authorization: Optional[str] = None, x_api_key: Optional[str] = None):
    token = x_api_key
    if not token and authorization:
        token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")
    # Will be validated async in the endpoint
    return token


# ─── Content Extraction ───────────────────────────────────────────────


def extract_user_content(messages: list, system=None, tools=None) -> str:
    parts = []
    if system:
        if isinstance(system, str):
            parts.append(f"[System]\n{system}\n")
        elif isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
            if sys_text:
                parts.append(f"[System]\n{sys_text}\n")

    # Include tool definitions so the model knows about available tools
    if tools:
        tools_desc = []
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            schema = tool.get("input_schema", {})
            tools_desc.append(f"### {name}\n{desc}\nInput schema: {json.dumps(schema, ensure_ascii=False)}")
        parts.append(f"[Available Tools]\n" + "\n\n".join(tools_desc) + "\n")
        parts.append(
            "[Tool Use Instructions]\n"
            "When you need to use a tool, output a tool_use block in this exact JSON format on its own line:\n"
            '```tool_use\n{"id": "toolu_<unique_id>", "name": "<tool_name>", "input": {<parameters>}}\n```\n'
            "You can output multiple tool_use blocks. After all tool_use blocks, do NOT output additional text.\n"
            "If you don't need any tool, just respond with plain text.\n"
        )

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}\n")
        elif isinstance(content, list):
            msg_parts = []
            for block in content:
                if isinstance(block, str):
                    msg_parts.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        msg_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        msg_parts.append(
                            f'```tool_use\n{json.dumps({"id": block.get("id",""), "name": block.get("name",""), "input": block.get("input",{})}, ensure_ascii=False)}\n```'
                        )
                    elif btype == "tool_result":
                        tool_content = block.get("content", "")
                        if isinstance(tool_content, list):
                            tool_content = " ".join(
                                b.get("text", "") for b in tool_content if isinstance(b, dict) and b.get("type") == "text"
                            )
                        msg_parts.append(f"[tool_result for {block.get('tool_use_id','')}]\n{tool_content}")
                    elif btype == "thinking":
                        pass  # skip thinking blocks
            if msg_parts:
                parts.append(f"[{role}]\n" + "\n".join(msg_parts) + "\n")

    return "\n".join(parts)


def append_style_hint(content: str, style_hint: str | None = None) -> str:
    if not style_hint:
        return content
    return f"[System Style Override]\n{style_hint}\n\n{content}"


def extract_session_key(request: Request, metadata: Optional[dict] = None) -> str:
    metadata = metadata or {}
    candidates = [
        request.headers.get("x-claude-code-session-id"),
        request.headers.get("x-session-id"),
        request.headers.get("x-conversation-id"),
        metadata.get("session_id"),
        metadata.get("conversation_id"),
        metadata.get("thread_id"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def is_claude_code_request(request: Request, metadata: Optional[dict] = None) -> bool:
    metadata = metadata or {}
    user_agent = (request.headers.get("user-agent") or "").lower()
    return (
        request.query_params.get("beta") == "true"
        or "claude" in user_agent
        or bool(request.headers.get("x-claude-code-session-id"))
        or metadata.get("client") == "claude-code"
    )


def get_response_style_hint(request: Request, metadata: Optional[dict] = None) -> str:
    if not is_claude_code_request(request, metadata):
        return ""
    return (
        "自然、直接、简短地回答。"
        "默认使用短段落，不要动不动就列清单、分标题、做评分、做能力罗列。"
        "简单问题用 1 到 3 段话直接回答后就收住。"
        "只有在用户明确要求列出项目，或者确实必须枚举时，才使用列表。"
        "如果用户问“你能做什么”或“你有哪些工具”，先用一句高层概括回答，不要默认把所有工具逐条列出来。"
        "整体语气像实用的编程助手，不像正式报告。"
    )


def _normalize_seed_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _extract_first_user_seed(messages: list) -> str:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
    return ""


def derive_stable_prompt_cache_key(
    model: str,
    messages: list,
    system=None,
    tools=None,
    style_hint: str = "",
    session_key: str = "",
) -> str:
    seed_parts = [f"model={model.strip().lower()}"]
    if session_key:
        seed_parts.append(f"session={session_key}")
    if style_hint:
        seed_parts.append("style=" + _normalize_seed_value(style_hint))
    if system:
        seed_parts.append("system=" + _normalize_seed_value(system))
    if tools:
        simplified_tools = [
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {}),
            }
            for tool in tools
        ]
        seed_parts.append("tools=" + _normalize_seed_value(simplified_tools))
    first_user = _extract_first_user_seed(messages)
    if first_user:
        seed_parts.append("first_user=" + first_user)
    digest = hashlib.sha256("|".join(seed_parts).encode("utf-8")).hexdigest()[:32]
    return f"compat_cc_{digest}"


def summarize_blob(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 64:
        return text[:max_chars]
    head = max_chars // 2
    tail = max_chars - head - 32
    omitted = max(0, len(text) - head - tail)
    return f"{text[:head]}\n...[omitted {omitted} chars]...\n{text[-tail:]}"


def extract_context_signals(text: str, max_items: int = 12) -> dict:
    file_paths = []
    for match in FILE_PATH_RE.findall(text or ""):
        normalized = match.replace("\\", "/")
        if normalized not in file_paths:
            file_paths.append(normalized)
        if len(file_paths) >= max_items:
            break

    tool_names = []
    for match in re.finditer(r'"name"\s*:\s*"([^"]+)"', text or ""):
        name = match.group(1)
        if name not in tool_names:
            tool_names.append(name)
        if len(tool_names) >= max_items:
            break

    return {
        "file_paths": file_paths,
        "tool_names": tool_names,
        "char_count": len(text or ""),
        "line_count": (text or "").count("\n") + 1 if text else 0,
    }


def compress_tool_result_content(text: str, max_chars: int = 600) -> str:
    signals = extract_context_signals(text)
    summary_parts = [
        f"[tool_result summary] chars={signals['char_count']} lines={signals['line_count']}",
    ]
    if signals["file_paths"]:
        summary_parts.append("files: " + ", ".join(signals["file_paths"]))
    if signals["tool_names"]:
        summary_parts.append("tools: " + ", ".join(signals["tool_names"]))
    summary_parts.append("excerpt:\n" + summarize_blob(text, max_chars))
    return "\n".join(summary_parts)


def _extract_tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _normalize_doc_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip()


def _extract_latest_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""


def _extract_file_documents(messages: list, limit: int = 10) -> list[dict]:
    tool_registry = {}
    documents = []

    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_registry[block.get("id", "")] = {
                    "name": block.get("name", ""),
                    "input": block.get("input", {}) or {},
                }
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                tool = tool_registry.get(tool_id, {})
                tool_name = (tool.get("name", "") or "").lower()
                if tool_name not in {"read", "read_file", "open_file"}:
                    continue
                tool_input = tool.get("input", {}) or {}
                path = _normalize_doc_path(
                    tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filepath") or ""
                )
                if not path:
                    continue
                text = _extract_tool_text(block.get("content", ""))
                if not text:
                    continue
                documents.append({
                    "path": path,
                    "basename": os.path.basename(path),
                    "tool_name": tool.get("name", ""),
                    "content": text,
                    "excerpt": summarize_blob(text, 4000),
                    "char_count": len(text),
                })

    deduped = []
    seen = set()
    for item in reversed(documents):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _merge_session_documents(stored: list[dict], current: list[dict], limit: int = 20) -> list[dict]:
    merged = []
    seen = set()
    for item in current + stored:
        path = _normalize_doc_path(item.get("path", ""))
        if not path or path in seen:
            continue
        item = dict(item)
        item["path"] = path
        item["basename"] = item.get("basename") or os.path.basename(path)
        item["excerpt"] = item.get("excerpt") or summarize_blob(item.get("content", ""), 4000)
        item["char_count"] = item.get("char_count") or len(item.get("content", ""))
        merged.append(item)
        seen.add(path)
        if len(merged) >= limit:
            break
    return merged


def _select_hot_documents(documents: list[dict], latest_user_text: str, current_paths: set[str], limit: int = 4) -> list[dict]:
    latest_lower = (latest_user_text or "").lower()
    selected = []
    seen = set()

    def _maybe_add(item: dict):
        path = item.get("path")
        if not path or path in seen:
            return
        seen.add(path)
        selected.append(item)

    for item in documents:
        if item.get("path") in current_paths:
            _maybe_add(item)
            if len(selected) >= limit:
                return selected

    for item in documents:
        basename = (item.get("basename") or "").lower()
        path_lower = (item.get("path") or "").lower()
        if basename and basename in latest_lower or path_lower and path_lower in latest_lower:
            _maybe_add(item)
            if len(selected) >= limit:
                return selected

    for item in documents:
        _maybe_add(item)
        if len(selected) >= limit:
            return selected

    return selected


def _extract_recent_file_contexts(messages: list, limit: int = 5) -> list[dict]:
    tool_registry = {}
    contexts = []

    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_registry[block.get("id", "")] = {
                    "name": block.get("name", ""),
                    "input": block.get("input", {}) or {},
                }
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                tool = tool_registry.get(tool_id, {})
                tool_name = (tool.get("name", "") or "").lower()
                if tool_name not in {"read", "read_file", "open_file"}:
                    continue
                tool_input = tool.get("input", {}) or {}
                path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filepath") or ""
                if not path:
                    continue
                text = _extract_tool_text(block.get("content", ""))
                if not text:
                    continue
                contexts.append({
                    "path": path.replace("\\", "/"),
                    "tool_name": tool.get("name", ""),
                    "excerpt": summarize_blob(text, 4000),
                    "char_count": len(text),
                })

    deduped = []
    seen = set()
    for item in reversed(contexts):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _merge_working_sets(stored: list[dict], current: list[dict], limit: int = 5) -> list[dict]:
    merged = []
    seen = set()
    for item in current + stored:
        path = item.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def render_working_set_section(items: list[dict]) -> str:
    if not items:
        return ""
    sections = ["[Session Working Set]\nRecently referenced files kept in context:\n"]
    for item in items[:5]:
        body = item.get("content", "")
        rendered_body = body if body and len(body) <= 6000 else summarize_blob(body or item.get("excerpt", ""), 6000)
        sections.append(
            f"### {item.get('path','')}\n"
            f"chars={item.get('char_count', 0)}\n"
            f"{rendered_body}\n"
        )
    return "\n".join(sections)


def summarize_tool_schema(schema: dict, max_chars: int = 1200) -> str:
    try:
        raw = json.dumps(schema or {}, ensure_ascii=False)
    except TypeError:
        return "{}"
    if len(raw) <= max_chars:
        return raw
    properties = (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}
    required = (schema or {}).get("required", []) if isinstance(schema, dict) else []
    summary = {
        "type": (schema or {}).get("type", "object") if isinstance(schema, dict) else "object",
        "properties": {
            key: {
                "type": (value or {}).get("type", "unknown"),
                "description": summarize_blob((value or {}).get("description", ""), 120),
            }
            for key, value in list(properties.items())[:20]
        },
        "required": required[:20],
    }
    return json.dumps(summary, ensure_ascii=False)


def render_tools_content(tools: Optional[list], compact: bool = False) -> list[str]:
    if not tools:
        return []

    tool_sections = []
    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})
        schema_text = summarize_tool_schema(schema, 800 if compact else 4000)
        tool_sections.append(f"### {name}\n{summarize_blob(desc, 400 if compact else 1200)}\nInput schema: {schema_text}")

    return [
        "[Available Tools]\n" + "\n\n".join(tool_sections) + "\n",
        "[Tool Use Instructions]\n"
        "When you need to use a tool, output a tool_use block in this exact JSON format on its own line:\n"
        '```tool_use\n{"id": "toolu_<unique_id>", "name": "<tool_name>", "input": {<parameters>}}\n```\n'
        "You can output multiple tool_use blocks. After all tool_use blocks, do NOT output additional text.\n"
        "If you don't need any tool, just respond with plain text.\n",
    ]


def render_message_content(
    msg: dict,
    compact: bool = False,
    cached_file_paths: set[str] | None = None,
    tool_registry: dict | None = None,
) -> str:
    role = msg.get("role", "user")
    content = msg.get("content", "")
    block_limit = 800 if compact else 12000
    cached_file_paths = cached_file_paths or set()
    tool_registry = tool_registry if tool_registry is not None else {}

    if isinstance(content, str):
        return f"[{role}]\n{summarize_blob(content, block_limit)}\n"

    msg_parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                msg_parts.append(summarize_blob(block, block_limit))
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    msg_parts.append(summarize_blob(block.get("text", ""), block_limit))
                elif btype == "tool_use":
                    tool_registry[block.get("id", "")] = {
                        "name": block.get("name", ""),
                        "input": block.get("input", {}) or {},
                    }
                    msg_parts.append(
                        f'```tool_use\n{json.dumps({"id": block.get("id",""), "name": block.get("name",""), "input": block.get("input",{})}, ensure_ascii=False)}\n```'
                    )
                elif btype == "tool_result":
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        tool_content = " ".join(
                            b.get("text", "") for b in tool_content if isinstance(b, dict) and b.get("type") == "text"
                        )
                    tool = tool_registry.get(block.get("tool_use_id", ""), {})
                    tool_name = (tool.get("name", "") or "").lower()
                    tool_input = tool.get("input", {}) or {}
                    path = _normalize_doc_path(
                        tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filepath") or ""
                    )
                    if path and tool_name in {"read", "read_file", "open_file"} and path in cached_file_paths:
                        tool_content = (
                            f"[cached file context for {path} already available in Session Working Set]\n"
                            f"Use that context unless the user asks to re-read the file."
                        )
                    elif compact:
                        tool_content = compress_tool_result_content(str(tool_content), 400)
                    msg_parts.append(
                        f"[tool_result for {block.get('tool_use_id','')}]\n{summarize_blob(str(tool_content), 700 if compact else 8000)}"
                    )

    return f"[{role}]\n" + "\n".join(part for part in msg_parts if part) + "\n"


def build_upstream_content(
    messages: list,
    system=None,
    tools=None,
    max_chars: int = MAX_UPSTREAM_MESSAGE_CHARS,
    style_hint: str = "",
    session_context_section: str = "",
    cached_file_paths: set[str] | None = None,
) -> tuple[str, bool]:
    cached_file_paths = cached_file_paths or set()
    full_parts = []
    if style_hint:
        full_parts.append("[System Style Override]\n" + style_hint + "\n")
    if system:
        if isinstance(system, str):
            full_parts.append(f"[System]\n{system}\n")
        elif isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
            if sys_text:
                full_parts.append(f"[System]\n{sys_text}\n")
    full_parts.extend(render_tools_content(tools, compact=False))
    if session_context_section:
        full_parts.append(session_context_section)
    tool_registry = {}
    for msg in messages:
        full_parts.append(
            render_message_content(
                msg,
                compact=False,
                cached_file_paths=cached_file_paths,
                tool_registry=tool_registry,
            )
        )
    full = "\n".join(part for part in full_parts if part)
    if len(full) <= max_chars:
        return full, False

    parts = []
    if system:
        if isinstance(system, str):
            parts.append(f"[System]\n{summarize_blob(system, 12000)}\n")
        elif isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
            if sys_text:
                parts.append(f"[System]\n{summarize_blob(sys_text, 12000)}\n")

    parts.extend(render_tools_content(tools, compact=True))
    if style_hint:
        parts.append(f"[Proxy Response Style]\n{style_hint}\n")
    if session_context_section:
        parts.append(session_context_section)

    rendered_messages = []
    recent_window = 6
    tool_registry = {}
    for idx, msg in enumerate(messages):
        compact = idx < max(0, len(messages) - recent_window)
        rendered_messages.append(
            render_message_content(
                msg,
                compact=compact,
                cached_file_paths=cached_file_paths,
                tool_registry=tool_registry,
            )
        )

    static_prefix = "\n".join(parts)
    remaining_budget = max_chars - len(static_prefix) - 256
    selected = []
    omitted = 0
    for section in reversed(rendered_messages):
        if len(section) <= remaining_budget or not selected:
            selected.append(section)
            remaining_budget -= len(section) + 1
        else:
            omitted += 1

    if omitted:
        parts.append(
            f"[Proxy Context Compression]\n"
            f"Compacted or omitted {omitted} older message blocks to fit Anything's {max_chars} character limit. "
            f"Prefer re-reading files with tools when details are needed.\n"
        )

    parts.extend(reversed(selected))
    compacted = "\n".join(part for part in parts if part)
    if len(compacted) > max_chars:
        compacted = summarize_blob(compacted, max_chars - 64)
    return compacted[:max_chars], True


# ─── Response Building ────────────────────────────────────────────────

TOOL_USE_RE = re.compile(
    r'```tool_use\s*\n(.*?)\n```',
    re.DOTALL,
)
FILE_PATH_RE = re.compile(r'(?:(?:[A-Za-z]:)?[\\/])?[A-Za-z0-9_.\-\\/]+\.[A-Za-z0-9]{1,12}')


def parse_tool_use_blocks(text: str) -> tuple[list[dict], str]:
    """Extract tool_use blocks from response text.
    Returns (tool_use_blocks, remaining_text)."""
    tool_blocks = []
    for match in TOOL_USE_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            tool_blocks.append({
                "type": "tool_use",
                "id": data.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": data.get("name", ""),
                "input": data.get("input", {}),
            })
        except json.JSONDecodeError:
            pass

    remaining = TOOL_USE_RE.sub("", text).strip()
    return tool_blocks, remaining


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_usage(usage: Optional[dict] = None) -> dict:
    usage = usage or {}
    return {
        "input_tokens": _safe_int(usage.get("input_tokens", 0)),
        "output_tokens": _safe_int(usage.get("output_tokens", 0)),
        "cache_creation_input_tokens": _safe_int(usage.get("cache_creation_input_tokens", 0)),
        "cache_read_input_tokens": _safe_int(usage.get("cache_read_input_tokens", 0)),
    }


def is_request_too_large_error(error: str) -> bool:
    lower = (error or "").lower()
    return (
        "maximum length of 200000 characters" in lower
        or "chat message exceeds the maximum length" in lower
        or "bad_user_input" in lower and "maximum length" in lower
    )


def build_response(model: str, thinking: Optional[str], text: str, usage: dict = None) -> dict:
    usage = normalize_usage(usage)
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})

    # Parse tool_use blocks from text
    tool_blocks, remaining_text = parse_tool_use_blocks(text)

    if remaining_text:
        content.append({"type": "text", "text": remaining_text})
    if tool_blocks:
        content.extend(tool_blocks)

    # If no content at all, add empty text
    if not content:
        content.append({"type": "text", "text": ""})

    stop_reason = "tool_use" if tool_blocks else "end_turn"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# ─── SSE Helpers ──────────────────────────────────────────────────────


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def chunk_text(text: str, size: int = 50):
    for i in range(0, len(text), size):
        yield text[i : i + size]


class LiveSSEAdapter:
    """Incrementally convert upstream text deltas into Anthropic SSE events.

    Tool-use blocks are buffered until a full ```tool_use fenced block is
    complete, while plain text is streamed immediately.
    """

    TOOL_USE_START = "```tool_use\n"

    def __init__(self, model: str, usage: Optional[dict] = None):
        self.model = model
        self.msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.input_usage = normalize_usage(usage)
        self.index = 0
        self.text_block_open = False
        self.pending_text = ""
        self.in_tool_block = False
        self.tool_buffer = ""
        self.saw_tool_use = False
        self.emitted_any_content = False

    def message_start(self) -> str:
        return sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": self.input_usage,
            },
        })

    def _text_start(self) -> list[str]:
        if self.text_block_open:
            return []
        self.text_block_open = True
        self.emitted_any_content = True
        return [sse("content_block_start", {
            "type": "content_block_start",
            "index": self.index,
            "content_block": {"type": "text", "text": ""},
        })]

    def _text_stop(self) -> list[str]:
        if not self.text_block_open:
            return []
        self.text_block_open = False
        event = sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        self.index += 1
        return [event]

    def _emit_text(self, text: str) -> list[str]:
        if not text:
            return []
        events = self._text_start()
        for ch in text:
            events.append(sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "text_delta", "text": ch},
            }))
        return events

    def _emit_tool_use(self, raw_json: str) -> list[str]:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            # Fallback: if the fenced block is malformed, surface it as plain text.
            return self._emit_text(f"```tool_use\n{raw_json}\n```")

        events = self._text_stop()
        self.emitted_any_content = True
        tool_id = data.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
        tool_name = data.get("name", "")
        tool_input = data.get("input", {})
        events.append(sse("content_block_start", {
            "type": "content_block_start",
            "index": self.index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
        }))
        events.append(sse("content_block_delta", {
            "type": "content_block_delta",
            "index": self.index,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input, ensure_ascii=False)},
        }))
        events.append(sse("content_block_stop", {"type": "content_block_stop", "index": self.index}))
        self.index += 1
        self.saw_tool_use = True
        return events

    def feed_text(self, delta: str) -> list[str]:
        if not delta:
            return []

        events: list[str] = []
        if self.in_tool_block:
            self.tool_buffer += delta
        else:
            self.pending_text += delta

        while True:
            if self.in_tool_block:
                match = TOOL_USE_RE.match(self.tool_buffer)
                if not match:
                    break
                events.extend(self._emit_tool_use(match.group(1)))
                self.tool_buffer = self.tool_buffer[match.end():]
                self.in_tool_block = False
                if self.tool_buffer:
                    self.pending_text += self.tool_buffer
                    self.tool_buffer = ""
                continue

            marker_pos = self.pending_text.find(self.TOOL_USE_START)
            if marker_pos != -1:
                events.extend(self._emit_text(self.pending_text[:marker_pos]))
                events.extend(self._text_stop())
                self.in_tool_block = True
                self.tool_buffer = self.pending_text[marker_pos:]
                self.pending_text = ""
                continue

            safe_len = max(0, len(self.pending_text) - (len(self.TOOL_USE_START) - 1))
            if safe_len <= 0:
                break
            events.extend(self._emit_text(self.pending_text[:safe_len]))
            self.pending_text = self.pending_text[safe_len:]
            break

        return events

    def finish(self, usage: Optional[dict] = None, error_text: str = "") -> list[str]:
        events: list[str] = []

        if error_text:
            if self.in_tool_block:
                self.pending_text += self.tool_buffer
                self.tool_buffer = ""
                self.in_tool_block = False
            self.pending_text += error_text

        if self.in_tool_block:
            self.pending_text += self.tool_buffer
            self.tool_buffer = ""
            self.in_tool_block = False

        if self.pending_text:
            events.extend(self._emit_text(self.pending_text))
            self.pending_text = ""

        if not self.emitted_any_content:
            events.extend(self._text_start())

        events.extend(self._text_stop())

        final_usage = normalize_usage(usage)
        stop_reason = "tool_use" if self.saw_tool_use else "end_turn"
        events.append(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": final_usage["output_tokens"]},
        }))
        events.append(sse("message_stop", {"type": "message_stop"}))
        return events


async def fake_stream(model: str, thinking: Optional[str], text: str, usage: dict = None):
    """Convert a complete response into SSE stream, with tool_use support."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    usage = normalize_usage(usage)

    # Parse tool_use blocks first
    tool_blocks, remaining_text = parse_tool_use_blocks(text)
    stop_reason = "tool_use" if tool_blocks else "end_turn"

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": usage,
        },
    })

    idx = 0
    if thinking:
        yield sse("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "thinking", "thinking": ""},
        })
        for c in chunk_text(thinking, 100):
            yield sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "thinking_delta", "thinking": c},
            })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        idx += 1

    if remaining_text:
        yield sse("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "text", "text": ""},
        })
        for c in chunk_text(remaining_text, 1):
            yield sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": c},
            })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        idx += 1

    for tb in tool_blocks:
        yield sse("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tb["id"], "name": tb["name"], "input": {}},
        })
        yield sse("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(tb["input"], ensure_ascii=False)},
        })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        idx += 1

    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage["output_tokens"]},
    })
    yield sse("message_stop", {"type": "message_stop"})


async def ws_stream(model: str, content: str, use_thinking: bool, account_id: int, client, tracker: UsageTracker, revision_id: str = None):
    """Stream via WebSocket and forward deltas to Anthropic SSE in real time."""
    full_text = ""
    error_text = ""
    adapter = LiveSSEAdapter(model, tracker.to_usage_dict())
    try:
        yield adapter.message_start()
        try:
            async for chunk in client.chat_stream(content, model, use_thinking, revision_id=revision_id):
                if chunk["type"] == "text" and chunk["content"]:
                    full_text += chunk["content"]
                    for event in adapter.feed_text(chunk["content"]):
                        yield event
                elif chunk["type"] == "error":
                    error_text = chunk["content"]
                    break
                elif chunk["type"] == "done":
                    break
            await account_pool.record_success(account_id)
        except Exception as e:
            await account_pool.record_failure(account_id, str(e))
            error_text = str(e)

        if error_text:
            full_text += f"\n[Error: {error_text}]"
            tracker.mark_error(error_text)
            error_suffix = f"\n[Error: {error_text}]"
        else:
            error_suffix = ""

        # Count output tokens
        tracker.count_output(full_text, None)
        if adapter.saw_tool_use:
            tracker.mark_tool_use()

        usage = tracker.to_usage_dict()
        for event in adapter.finish(usage, error_suffix):
            yield event
    finally:
        # Save usage log and release the leased account even if the client disconnects mid-stream.
        await tracker.save()
        await account_pool.release_account(account_id)


# ─── Endpoints ─────────────────────────────────────────────────────────


async def _validate_api_key(token: str):
    """Validate API key against database or config. Update last_used_at."""
    # Check config-level key first
    if settings.api_key and token == settings.api_key:
        return True
    # Check database keys
    key_row = await fetchone(
        "SELECT id FROM api_keys WHERE key = ? AND is_active = 1", (token,)
    )
    if key_row:
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        await execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, key_row["id"]))
        return True
    # If no keys configured at all (no config key + no DB keys), allow open access
    if not settings.api_key:
        key_count = await fetchone("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1")
        if key_count and key_count["cnt"] == 0:
            return True
    return False


@router.post("/v1/messages")
async def messages(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    token = x_api_key
    if not token and authorization:
        token = (authorization.removeprefix("Bearer ").strip()) if authorization else ""
    if token:
        if not await _validate_api_key(token):
            raise HTTPException(status_code=401, detail="Invalid API key")
    else:
        # No key provided — check if keys are configured
        if settings.api_key:
            raise HTTPException(status_code=401, detail="Missing API key")
        key_count = await fetchone("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1")
        if key_count and key_count["cnt"] > 0:
            raise HTTPException(status_code=401, detail="Missing API key")

    body = await request.json()
    req = MessagesRequest(**body)

    use_thinking = req.thinking is not None and req.thinking.type in ("enabled", "adaptive")
    session_key = extract_session_key(request, req.metadata)
    style_hint = get_response_style_hint(request, req.metadata)
    prompt_cache_key = derive_stable_prompt_cache_key(
        req.model,
        req.messages,
        system=req.system,
        tools=req.tools,
        style_hint=style_hint,
        session_key=session_key,
    )
    stored_documents = await get_session_documents(session_key)
    current_documents = _extract_file_documents(req.messages)
    merged_documents = _merge_session_documents(stored_documents, current_documents)
    if session_key and merged_documents != stored_documents:
        await set_session_documents(session_key, merged_documents, account_pool.SESSION_TTL_SECONDS)

    latest_user_text = _extract_latest_user_text(req.messages)
    hot_documents = _select_hot_documents(
        merged_documents,
        latest_user_text,
        {doc["path"] for doc in current_documents},
    )
    working_set_items = [
        {
            "path": doc.get("path", ""),
            "tool_name": doc.get("tool_name", ""),
            "excerpt": doc.get("excerpt", ""),
            "char_count": doc.get("char_count", 0),
        }
        for doc in hot_documents
    ]
    stored_working_set = await get_session_working_set(session_key)
    if session_key and working_set_items != stored_working_set:
        await set_session_working_set(session_key, working_set_items, account_pool.SESSION_TTL_SECONDS)

    session_context_section = render_working_set_section(hot_documents)
    cached_file_paths = {doc.get("path", "") for doc in hot_documents if doc.get("path")}
    content, compacted = build_upstream_content(
        req.messages,
        req.system,
        req.tools,
        style_hint=style_hint,
        session_context_section=session_context_section,
        cached_file_paths=cached_file_paths,
    )

    if not content.strip():
        raise HTTPException(status_code=400, detail="No message content")
    if len(content) > MAX_UPSTREAM_MESSAGE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Message content length {len(content)} exceeds Anything's limit of "
                f"{MAX_UPSTREAM_MESSAGE_CHARS} characters."
            ),
        )
    if compacted:
        logger.warning(
            f"Applied proxy context compression for request: compacted_length={len(content)} "
            f"limit={MAX_UPSTREAM_MESSAGE_CHARS}"
        )

    # Pick an account with retry on quota exhaustion
    max_retries = 10
    tried_ids = set()
    last_error = None
    preferred_account_id = await account_pool.get_bound_account(session_key)

    for attempt in range(max_retries):
        try:
            account_id, client = await account_pool.pick_account(
                exclude_ids=tried_ids,
                preferred_account_id=preferred_account_id,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

        tried_ids.add(account_id)

        # Create usage tracker
        tracker = UsageTracker(
            model=req.model, is_stream=req.stream,
            account_id=account_id, api_key=token or "",
        )
        await tracker.count_input(content, cache_key=prompt_cache_key)

        if req.stream:
            # For streaming: try send_message first within retry loop
            from anything_client import get_mapped_model
            mapped_model = get_mapped_model(req.model)
            stream_started = False
            try:
                revision_id = await client.send_message(content, mapped_model, use_thinking)
                logger.info(f"Stream: message sent on account {account_id}, revision_id={revision_id}")
                stream_started = True
                if session_key:
                    await account_pool.bind_session(session_key, account_id)
                # Hold the account lease until ws_stream finishes to avoid concurrent client state mutation.
                return StreamingResponse(
                    ws_stream(req.model, content, use_thinking, account_id, client, tracker, revision_id),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                )
            except Exception as e:
                error_str = str(e)
                if is_request_too_large_error(error_str):
                    logger.warning(f"Stream: request rejected before retry due to oversized content ({len(content)} chars)")
                    tracker.mark_error(error_str)
                    asyncio.ensure_future(tracker.save())
                    raise HTTPException(status_code=400, detail=error_str)
                await account_pool.record_failure(account_id, error_str)
                if account_pool.is_quota_error(error_str) and attempt < max_retries - 1:
                    if session_key:
                        await account_pool.unbind_session(session_key)
                    await account_pool.mark_quota_exhausted(account_id)
                    logger.warning(f"Stream: account {account_id} quota exhausted, trying next (attempt {attempt+1})")
                    last_error = e
                    await asyncio.sleep(2)  # Delay before retrying to avoid IP rate limiting
                    preferred_account_id = None
                    continue
                if account_pool.is_retryable_account_error(error_str) and attempt < max_retries - 1:
                    if session_key:
                        await account_pool.unbind_session(session_key)
                    logger.warning(
                        f"Stream: account {account_id} permission/project-group error, trying next (attempt {attempt+1})"
                    )
                    last_error = e
                    await asyncio.sleep(1)
                    preferred_account_id = None
                    continue
                if account_pool.is_permission_blocked_error(error_str) and attempt < max_retries - 1:
                    if session_key:
                        await account_pool.unbind_session(session_key)
                    await account_pool.mark_permission_blocked(account_id, error_str)
                    logger.warning(
                        f"Stream: account {account_id} forbidden to create chat messages, quarantined and trying next (attempt {attempt+1})"
                    )
                    last_error = e
                    await asyncio.sleep(1)
                    preferred_account_id = None
                    continue
                logger.error(f"Stream: send failed (account {account_id}): {e}")
                tracker.mark_error(error_str)
                asyncio.ensure_future(tracker.save())
                raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
            finally:
                if not stream_started:
                    await account_pool.release_account(account_id)

        # Non-streaming: poll with retry
        try:
            thinking, text, meta = await client.chat(content, req.model, use_thinking)
            await account_pool.record_success(account_id)

            # Count output tokens and build response
            tracker.count_output(text, thinking)
            tool_blocks, _ = parse_tool_use_blocks(text)
            if tool_blocks:
                tracker.mark_tool_use()

            usage = tracker.to_usage_dict()
            resp = build_response(req.model, thinking, text, usage)
            tracker.set_request_id(resp["id"])
            if session_key:
                await account_pool.bind_session(session_key, account_id)

            # Save usage log (fire and forget)
            asyncio.ensure_future(tracker.save())

            return JSONResponse(content=resp)
        except ValueError as e:
            tracker.mark_error(str(e))
            asyncio.ensure_future(tracker.save())
            raise HTTPException(status_code=400, detail=str(e))
        except TimeoutError as e:
            await account_pool.record_failure(account_id, str(e))
            tracker.mark_error(str(e))
            asyncio.ensure_future(tracker.save())
            raise HTTPException(status_code=504, detail=str(e))
        except Exception as e:
            error_str = str(e)
            if is_request_too_large_error(error_str):
                logger.warning(f"Request rejected before retry due to oversized content ({len(content)} chars)")
                tracker.mark_error(error_str)
                asyncio.ensure_future(tracker.save())
                raise HTTPException(status_code=400, detail=error_str)
            await account_pool.record_failure(account_id, error_str)

            # If it's a quota error, mark account and retry with another
            if account_pool.is_quota_error(error_str) and attempt < max_retries - 1:
                if session_key:
                    await account_pool.unbind_session(session_key)
                await account_pool.mark_quota_exhausted(account_id)
                logger.warning(f"Account {account_id} quota exhausted, trying next account (attempt {attempt+1})")
                last_error = e
                await asyncio.sleep(2)
                preferred_account_id = None
                continue
            if account_pool.is_permission_blocked_error(error_str) and attempt < max_retries - 1:
                if session_key:
                    await account_pool.unbind_session(session_key)
                await account_pool.mark_permission_blocked(account_id, error_str)
                logger.warning(
                    f"Account {account_id} forbidden to create chat messages, quarantined and trying next account (attempt {attempt+1})"
                )
                last_error = e
                await asyncio.sleep(1)
                preferred_account_id = None
                continue
            if account_pool.is_retryable_account_error(error_str) and attempt < max_retries - 1:
                if session_key:
                    await account_pool.unbind_session(session_key)
                logger.warning(
                    f"Account {account_id} permission/project-group error, trying next account (attempt {attempt+1})"
                )
                last_error = e
                await asyncio.sleep(1)
                preferred_account_id = None
                continue

            logger.error(f"Chat failed (account {account_id}): {e}", exc_info=True)
            tracker.mark_error(error_str)
            asyncio.ensure_future(tracker.save())
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
        finally:
            await account_pool.release_account(account_id)

    # All retries exhausted
    raise HTTPException(status_code=502, detail=f"All accounts exhausted: {last_error}")


@router.post("/v1/messages/count_tokens")
async def count_tokens_message(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    token = x_api_key
    if not token and authorization:
        token = (authorization.removeprefix("Bearer ").strip()) if authorization else ""
    if token:
        if not await _validate_api_key(token):
            raise HTTPException(status_code=401, detail="Invalid API key")
    else:
        if settings.api_key:
            raise HTTPException(status_code=401, detail="Missing API key")
        key_count = await fetchone("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1")
        if key_count and key_count["cnt"] > 0:
            raise HTTPException(status_code=401, detail="Missing API key")

    body = await request.json()
    req = MessagesRequest(**body)
    content = extract_user_content(req.messages, req.system, req.tools)
    return JSONResponse(content={"input_tokens": count_tokens(content)})


@router.get("/v1/models")
async def list_models():
    data = [
        {
            "type": "model",
            "id": m,
            "display_name": m,
            "created_at": "2025-01-01T00:00:00Z",
        }
        for m in SUPPORTED_MODELS
    ]
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        # Keep a compatibility field for older clients already using this proxy.
        "models": [{"id": m["id"], "object": "model"} for m in data],
    }


@router.get("/health")
async def health():
    from database.connection import fetchone
    count = await fetchone("SELECT COUNT(*) as cnt FROM accounts WHERE is_active = 1 AND status = 'active'")
    return {"status": "ok", "active_accounts": count["cnt"] if count else 0}
