# SiliconFlow VL 图片请求格式校准记录

日期：2026-10-25（阶段 4 开工时，解决阶段 0 遗留）
模型：`Qwen/Qwen3-VL-Embedding-8B`　端点：`POST https://api.siliconflow.cn/v1/embeddings`

## 结论（以实测为准）

| 请求体 input 写法 | 结果 |
|---|---|
| `{"type": "image_url", "image_url": "data:image/png;base64,…"}` | 400 code=20015 |
| `{"type": "image_url", "image_url": {"url": "data:…"}}`（OpenAI 兼容风格） | 400 code=20015 |
| `{"image": "<裸 base64>"}` | 400 code=20015 |
| `{"image": "https://公网图片URL"}` | 400 code=20015 |
| **`{"image": "data:image/png;base64,…"}`** | **200 ✓** |
| `{"type": "image", "image": "data:…"}` | 200 ✓（等价） |
| 纯文本 VL 体 `["你好世界"]` | 200 ✓ |
| 混合列表 `["文本", {"text": …}, {"image": "data:…"}]` + `dimensions: 1024` | 200 ✓，逐项返回向量 |

- `dimensions` 对图片与混合列表同样生效（1024 精确返回）→ 图文统一 1024 维向量空间成立。
- 官方文档页（EmbeddingsVLRequest）对 `input` 的描述为"URL 或 base64"，但实测**裸 base64 不行**，必须带 `data:` 前缀；公网 URL 也 400（可能与图片源抓取策略有关，未深究）。工程上一律用 data URL。
- 混合列表上限 32 项（官方文档标注）。

## 落地

`backend/app/services/embedding.py` 的 `embed_images()`：data URL 封装 + 与文本相同的
`dimensions`/重试/维度护栏逻辑；文本批量（Classic 格式）不变。
图文（页面截图/图片原件）入库为增量能力，P0 检索链路为纯文本。
