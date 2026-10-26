"""Embedding 客户端抽象（§9.8）。

默认实现 SiliconFlowClient：硅基流动 API + Qwen/Qwen3-VL-Embedding-8B。
- 文本走 Classic 批量格式（一次多条更省钱），带 429/5xx 指数退避；
- 维度使用已定稿的 EMBEDDING_DIM（1024，见 docs/experiments 探测报告）；
- 图片走 VL 格式 {"image": "<data-url>"}（实测校准通过，dimensions 同样生效，
  图文统一 1024 维向量空间；P0 仅文本入库，embed_images 供增量图文使用）；
- 查询与文档必须同模型同归一化（本客户端统一保证）。
"""

import asyncio
import base64
from abc import ABC, abstractmethod
from functools import lru_cache

import httpx

from app.core.config import settings

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 8


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_texts([text]))[0]

    @abstractmethod
    async def embed_images(self, images: list[bytes]) -> list[list[float]]: ...


def _data_url(image: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(image).decode("ascii")


class SiliconFlowClient(EmbeddingClient):
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 16,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise EmbeddingError("SILICONFLOW_API_KEY 未配置")
        self._base = api_base.rstrip("/")
        self._key = api_key
        self._model = model
        self._dim = dim
        self._batch_size = max(1, batch_size)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._request(texts[start : start + self._batch_size]))
        return vectors

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        # 实测校准（docs/experiments）：VL 图片内容对象为 {"image": "<data-url>"}，
        # base64 必须带 data: 前缀（裸 base64/公网 URL 均 400）。
        payload = [{"image": _data_url(img)} for img in images]
        return await self._request(payload)

    async def _request(self, input_payload: list) -> list[list[float]]:
        body: dict = {"model": self._model, "input": input_payload, "encoding_format": "float"}
        if self._dim:
            body["dimensions"] = self._dim

        attempt = 0
        while True:
            response = await self._client.post(
                f"{self._base}/embeddings",
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            if response.status_code == 200:
                data = sorted(response.json()["data"], key=lambda item: item["index"])
                vectors = [item["embedding"] for item in data]
                if len(vectors) != len(input_payload):
                    raise EmbeddingError(f"expected {len(input_payload)} vectors, got {len(vectors)}")
                if self._dim and any(len(v) != self._dim for v in vectors):
                    dims = {len(v) for v in vectors}
                    raise EmbeddingError(f"dimension mismatch: got {dims}, expected {self._dim}")
                return vectors
            if response.status_code in RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else min(2**attempt, 60)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            raise EmbeddingError(f"HTTP {response.status_code}: {response.text[:200]}")


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    return SiliconFlowClient(
        api_base=settings.siliconflow_api_base,
        api_key=settings.siliconflow_api_key,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        batch_size=settings.embedding_batch_size,
    )
