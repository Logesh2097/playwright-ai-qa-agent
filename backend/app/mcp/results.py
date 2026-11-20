"""Protocol-level view of a tool result."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImageBlock:
    mime_type: str
    data_base64: str


@dataclass(frozen=True, slots=True)
class RawToolResult:
    """What the server returned, before anything interprets it.

    `is_error` is the server's own flag. A result without it says only that the
    tool ran, not that it did what the caller wanted.
    """

    is_error: bool
    texts: tuple[str, ...]
    images: tuple[ImageBlock, ...] = ()
    other_blocks: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


def parse_tool_result(result: Any) -> RawToolResult:
    """Convert an SDK `CallToolResult`. Unknown block types are counted, not trusted."""
    texts: list[str] = []
    images: list[ImageBlock] = []
    other = 0
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text" and isinstance(getattr(block, "text", None), str):
            texts.append(block.text)
        elif kind == "image" and isinstance(getattr(block, "data", None), str):
            images.append(ImageBlock(str(getattr(block, "mime_type", "") or ""), block.data))
        else:
            other += 1
    return RawToolResult(
        is_error=bool(getattr(result, "is_error", False)),
        texts=tuple(texts),
        images=tuple(images),
        other_blocks=other,
    )
