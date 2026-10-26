"""LLM 客户端（§12.1 第 7 步）：OpenAI 兼容 chat/completions，支持 SSE 流式。

端点独立可配（llm_api_base/llm_api_key，缺省回落到硅基流动），可指向官方
DeepSeek API 等任何 OpenAI 兼容服务；Embedding 始终走硅基流动（§9.8）。
核心知识库功能不依赖本模块在线：所有失败以 LLMError 上抛，由 /qa 端点
降级为仅检索结果（§12.6）。
"""

import asyncio
import base64
import json
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import AsyncIterator

import httpx

from app.core.config import settings

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
# 429（限流/系统繁忙）需要比 5xx 更长的退避：实测短间隔重试会连续撞墙
_BASE_BACKOFF_SECONDS = 5.0


class LLMError(RuntimeError):
    pass


class LLMClient(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict]) -> str: ...

    @abstractmethod
    def stream(self, messages: list[dict]) -> AsyncIterator[str]: ...


def _data_url(image: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(image).decode("ascii")  # 图文混排增量使用


class OpenAICompatLLM(LLMClient):
    """任意 OpenAI 兼容 chat/completions 服务（硅基流动 / 官方 DeepSeek API / 本地 vLLM）。"""

    def __init__(self, api_base: str, api_key: str, model: str, timeout: float = 120.0):
        if not api_key:
            raise LLMError("LLM API Key 未配置")
        self._base = api_base.rstrip("/")
        self._key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=timeout)
        # 厂商私有参数（如 DeepSeek 关思考 {"thinking":{"type":"disabled"}}），合并进每次请求体
        self._extra_body: dict = {}
        if settings.llm_extra_body:
            try:
                self._extra_body = json.loads(settings.llm_extra_body)
            except ValueError as exc:
                raise LLMError(f"LLM_EXTRA_BODY 不是合法 JSON: {exc}") from exc

    def _build_body(self, messages: list[dict], stream: bool, max_tokens: int) -> dict:
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "temperature": settings.llm_temperature,
            "max_tokens": max_tokens,
        }
        body.update(self._extra_body)
        return body

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}"}

    async def chat(self, messages: list[dict], max_tokens: int | None = None) -> str:
        """非流式补全（追问改写等短调用），带 429/5xx 退避。"""
        body = self._build_body(messages, stream=False, max_tokens=max_tokens or settings.llm_max_tokens)
        attempt = 0
        while True:
            response = await self._client.post(
                f"{self._base}/chat/completions", json=body, headers=self._headers()
            )
            if response.status_code == 200:
                data = response.json()
                try:
                    return data["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError) as exc:
                    raise LLMError(f"unexpected response shape: {str(data)[:200]}") from exc
            if response.status_code in RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after) if retry_after
                    else min(_BASE_BACKOFF_SECONDS * (2**attempt), 60)
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}")

    async def stream(self, messages: list[dict], max_tokens: int | None = None) -> AsyncIterator[str]:
        """SSE 流式补全：逐段产出增量文本（跳过 reasoning_content 思考段）。"""
        body = self._build_body(messages, stream=True, max_tokens=max_tokens or settings.llm_max_tokens)
        try:
            async with self._client.stream(
                "POST", f"{self._base}/chat/completions", json=body, headers=self._headers()
            ) as response:
                if response.status_code != 200:
                    payload = (await response.aread()).decode("utf-8", "replace")
                    raise LLMError(f"HTTP {response.status_code}: {payload[:200]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                    except (ValueError, KeyError, IndexError):
                        continue
                    text = delta.get("content")
                    if text:
                        yield text
        except httpx.HTTPError as exc:
            raise LLMError(f"stream failed: {exc}") from exc


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    # base 与 key 必须成对：单独设置其一会导致"DeepSeek 端点 + 硅基流动 Key"式的混搭 401
    if settings.llm_api_base and settings.llm_api_key:
        base, key = settings.llm_api_base, settings.llm_api_key
    elif not settings.llm_api_base and not settings.llm_api_key:
        base, key = settings.siliconflow_api_base, settings.siliconflow_api_key
    else:
        raise LLMError("LLM_API_BASE 与 LLM_API_KEY 必须同时配置（或同时留空走硅基流动）")
    return OpenAICompatLLM(api_base=base, api_key=key, model=settings.llm_model)
