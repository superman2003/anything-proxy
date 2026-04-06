"""Optional Redis-backed runtime state with in-memory fallback."""

import asyncio
import json
import logging
import time

from config import settings

logger = logging.getLogger(__name__)

_memory_sessions: dict[str, tuple[int, float]] = {}
_session_lock = asyncio.Lock()
_redis = None
_memory_prompt_cache: dict[str, tuple[int, float]] = {}
_prompt_cache_lock = asyncio.Lock()
_memory_working_sets: dict[str, tuple[list[dict], float]] = {}
_working_set_lock = asyncio.Lock()
_memory_session_docs: dict[str, tuple[list[dict], float]] = {}
_session_docs_lock = asyncio.Lock()


def _session_key(session_key: str) -> str:
    return f"{settings.redis_prefix}:session:{session_key}"


def _prompt_cache_key(content_hash: str) -> str:
    return f"{settings.redis_prefix}:prompt_cache:{content_hash}"


def _working_set_key(session_key: str) -> str:
    return f"{settings.redis_prefix}:working_set:{session_key}"


def _session_docs_key(session_key: str) -> str:
    return f"{settings.redis_prefix}:session_docs:{session_key}"


async def _get_redis():
    global _redis
    if not settings.redis_url:
        return None
    if _redis is None:
        try:
            from redis.asyncio import Redis

            _redis = Redis.from_url(settings.redis_url, decode_responses=True)
        except Exception as e:
            logger.warning(f"Redis 初始化失败，回退内存状态: {e}")
            _redis = False
    return _redis if _redis is not False else None


async def set_session_account(session_key: str, account_id: int, ttl_seconds: int):
    if not session_key:
        return
    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.set(_session_key(session_key), str(account_id), ex=ttl_seconds)
            return
        except Exception as e:
            logger.warning(f"Redis 写入 session 绑定失败，回退内存: {e}")
    async with _session_lock:
        _memory_sessions[session_key] = (account_id, time.monotonic() + ttl_seconds)


async def get_session_account(session_key: str) -> int | None:
    if not session_key:
        return None
    redis = await _get_redis()
    if redis is not None:
        try:
            value = await redis.get(_session_key(session_key))
            return int(value) if value is not None else None
        except Exception as e:
            logger.warning(f"Redis 读取 session 绑定失败，回退内存: {e}")
    async with _session_lock:
        value = _memory_sessions.get(session_key)
        if not value:
            return None
        account_id, expires_at = value
        if time.monotonic() > expires_at:
            _memory_sessions.pop(session_key, None)
            return None
        return account_id


async def delete_session_account(session_key: str):
    if not session_key:
        return
    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.delete(_session_key(session_key))
        except Exception as e:
            logger.warning(f"Redis 删除 session 绑定失败，回退内存: {e}")
    async with _session_lock:
        _memory_sessions.pop(session_key, None)


async def get_prompt_cache(content_hash: str) -> int | None:
    redis = await _get_redis()
    if redis is not None:
        try:
            value = await redis.get(_prompt_cache_key(content_hash))
            return int(value) if value is not None else None
        except Exception as e:
            logger.warning(f"Redis 读取 prompt cache 失败，回退内存: {e}")
    async with _prompt_cache_lock:
        item = _memory_prompt_cache.get(content_hash)
        if not item:
            return None
        token_count, expires_at = item
        if time.monotonic() > expires_at:
            _memory_prompt_cache.pop(content_hash, None)
            return None
        return token_count


async def set_prompt_cache(content_hash: str, token_count: int, ttl_seconds: int):
    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.set(_prompt_cache_key(content_hash), str(token_count), ex=ttl_seconds)
            return
        except Exception as e:
            logger.warning(f"Redis 写入 prompt cache 失败，回退内存: {e}")
    async with _prompt_cache_lock:
        _memory_prompt_cache[content_hash] = (token_count, time.monotonic() + ttl_seconds)


async def get_session_working_set(session_key: str) -> list[dict]:
    if not session_key:
        return []
    redis = await _get_redis()
    if redis is not None:
        try:
            raw = await redis.get(_working_set_key(session_key))
            return json.loads(raw) if raw else []
        except Exception as e:
            logger.warning(f"Redis 读取 working set 失败，回退内存: {e}")
    async with _working_set_lock:
        item = _memory_working_sets.get(session_key)
        if not item:
            return []
        data, expires_at = item
        if time.monotonic() > expires_at:
            _memory_working_sets.pop(session_key, None)
            return []
        return data


async def set_session_working_set(session_key: str, items: list[dict], ttl_seconds: int):
    if not session_key:
        return
    trimmed = items[:5]
    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.set(_working_set_key(session_key), json.dumps(trimmed, ensure_ascii=False), ex=ttl_seconds)
            return
        except Exception as e:
            logger.warning(f"Redis 写入 working set 失败，回退内存: {e}")
    async with _working_set_lock:
        _memory_working_sets[session_key] = (trimmed, time.monotonic() + ttl_seconds)


async def get_session_documents(session_key: str) -> list[dict]:
    if not session_key:
        return []
    redis = await _get_redis()
    if redis is not None:
        try:
            raw = await redis.get(_session_docs_key(session_key))
            return json.loads(raw) if raw else []
        except Exception as e:
            logger.warning(f"Redis 读取 session docs 失败，回退内存: {e}")
    async with _session_docs_lock:
        item = _memory_session_docs.get(session_key)
        if not item:
            return []
        data, expires_at = item
        if time.monotonic() > expires_at:
            _memory_session_docs.pop(session_key, None)
            return []
        return data


async def set_session_documents(session_key: str, documents: list[dict], ttl_seconds: int):
    if not session_key:
        return
    trimmed = documents[:20]
    redis = await _get_redis()
    if redis is not None:
        try:
            await redis.set(_session_docs_key(session_key), json.dumps(trimmed, ensure_ascii=False), ex=ttl_seconds)
            return
        except Exception as e:
            logger.warning(f"Redis 写入 session docs 失败，回退内存: {e}")
    async with _session_docs_lock:
        _memory_session_docs[session_key] = (trimmed, time.monotonic() + ttl_seconds)


async def close_runtime_state():
    global _redis
    if _redis not in (None, False):
        try:
            await _redis.aclose()
        except Exception:
            pass
    _redis = None
