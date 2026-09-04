from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from company_brain.knowledge.markdown import MarkdownChunk


class EmbeddingProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingRecord:
    chunk_hash: str
    provider: str
    model: str
    vector: list[float]


class DeterministicEmbeddingProvider:
    provider_id = "deterministic-test"
    model_id = "sha256-v1"

    def __init__(self, dimensions: int = 8) -> None:
        if dimensions < 1 or dimensions > 32:
            raise ValueError("dimensions must be between 1 and 32")
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = sha256(text.encode()).digest()
            vectors.append([byte / 255 for byte in digest[: self.dimensions]])
        return vectors


def embed_chunks(
    chunks: list[MarkdownChunk], provider: EmbeddingProvider
) -> list[EmbeddingRecord]:
    vectors = provider.embed([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("Embedding provider returned an unexpected vector count")
    records: list[EmbeddingRecord] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        if len(vector) != provider.dimensions:
            raise ValueError("Embedding provider returned an unexpected dimension")
        records.append(
            EmbeddingRecord(
                chunk_hash=chunk.content_hash,
                provider=provider.provider_id,
                model=provider.model_id,
                vector=vector,
            )
        )
    return records