"""文本分块（§9.4 初始设置）：标题—段落—句子优先，目标 400–600 字、重叠 60–100 字。

初值为实验起点，后续依据检索效果调整（§9.4 明示非普适最优参数）。
"""

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;.])\s*")


def _split_long(piece: str, max_len: int) -> list[str]:
    """超长段按句子切，仍超长则硬切。"""
    parts: list[str] = []
    buf = ""
    for sentence in _SENTENCE_SPLIT.split(piece):
        if not sentence:
            continue
        while len(sentence) > max_len:  # 单句超长硬切
            parts.append(sentence[:max_len])
            sentence = sentence[max_len:]
        if len(buf) + len(sentence) <= max_len or not buf:
            buf += sentence
        else:
            parts.append(buf)
            buf = sentence
    if buf:
        parts.append(buf)
    return parts


def chunk_text(text: str, target: int = 450, max_len: int = 600, overlap: int = 80) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text or "") if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_len:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_long(paragraph, max_len))

    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = f"{buf}\n{piece}" if buf else piece
        if len(candidate) <= target or not buf:
            buf = candidate
        else:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap > 0 and len(buf) > overlap else ""
            # 重叠尾巴不得把 buf 顶过上限（否则中间块会超 max_len）
            if len(tail) + len(piece) > max_len:
                tail = tail[: max_len - len(piece)] if max_len > len(piece) else ""
            buf = f"{tail}{piece}" if tail else piece
    # buf 可能因重叠拼接超上限，硬切兜底
    while len(buf) > max_len:
        chunks.append(buf[:max_len])
        buf = buf[max_len:]
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]
