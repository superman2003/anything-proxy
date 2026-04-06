"""Token estimation and usage logging.

Uses tiktoken (cl100k_base) for Claude-compatible token counting.
Falls back to character-based estimation if tiktoken unavailable.

Also provides prompt caching estimation based on content hashing.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from database.connection import execute
from services.runtime_state import get_prompt_cache, set_prompt_cache

logger = logging.getLogger(__name__)

# ─── Token Counting ───────────────────────────────────────────────────

try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_encoder.encode(text))

except ImportError:
    logger.warning("tiktoken not installed, using character-based estimation (~4 chars/token)")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)


# ─── Prompt Cache ─────────────────────────────────────────────────────

_CACHE_MAX_SIZE = 256
_PROMPT_CACHE_TTL_SECONDS = 3600


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


async def estimate_cache_tokens(input_text: str) -> tuple[int, int, int]:
    """Estimate cache read/write tokens for input content.

    Anthropic caches prompts >= 1024 tokens with matching prefix.
    We simulate this by hashing the input and checking Redis/memory cache.

    Returns (input_tokens, cache_read_tokens, cache_write_tokens).
    """
    total_input = count_tokens(input_text)

    if total_input < 1024:
        return total_input, 0, 0

    h = _content_hash(input_text)

    cached_count = await get_prompt_cache(h)
    if cached_count is not None:
        # Cache hit: most tokens are read from cache
        cache_read = cached_count
        cache_write = 0
        remaining = max(0, total_input - cached_count)
        return remaining, cache_read, cache_write
    else:
        # Cache miss: write the prefix to cache
        # Anthropic caches the system prompt + tools portion
        cache_write = total_input
        await set_prompt_cache(h, total_input, _PROMPT_CACHE_TTL_SECONDS)

        return total_input, 0, cache_write


# ─── Usage Logging ────────────────────────────────────────────────────


class UsageTracker:
    """Track and log token usage for a single request."""

    def __init__(self, model: str, is_stream: bool = False, account_id: int = None, api_key: str = ""):
        self.request_id = ""
        self.model = model
        self.is_stream = is_stream
        self.account_id = account_id
        self.api_key = api_key
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.has_thinking = False
        self.has_tool_use = False
        self.status = "success"
        self.error_message = None
        self._start_time = time.monotonic()

    def set_request_id(self, request_id: str):
        self.request_id = request_id

    async def count_input(self, content: str):
        """Count input tokens with cache estimation."""
        input_tokens, cache_read, cache_write = await estimate_cache_tokens(content)
        self.input_tokens = input_tokens
        self.cache_read_tokens = cache_read
        self.cache_write_tokens = cache_write

    def count_output(self, text: str, thinking: Optional[str] = None):
        """Count output tokens."""
        self.output_tokens = count_tokens(text or "")
        if thinking:
            self.output_tokens += count_tokens(thinking)
            self.has_thinking = True

    def mark_tool_use(self):
        self.has_tool_use = True

    def mark_error(self, error: str):
        self.status = "error"
        self.error_message = error[:500]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)

    def to_usage_dict(self) -> dict:
        """Return Anthropic-compatible usage dict."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_write_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
        }

    async def save(self):
        """Persist usage log to database."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            await execute(
                "INSERT INTO usage_logs "
                "(request_id, account_id, api_key_id, model, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "total_tokens, is_stream, has_thinking, has_tool_use, "
                "status, error_message, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.request_id, self.account_id, self.api_key or "",
                    self.model,
                    self.input_tokens, self.output_tokens,
                    self.cache_read_tokens, self.cache_write_tokens,
                    self.total_tokens,
                    int(self.is_stream), int(self.has_thinking), int(self.has_tool_use),
                    self.status, self.error_message,
                    self.duration_ms, now,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to save usage log: {e}")
