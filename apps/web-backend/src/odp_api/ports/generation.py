"""Vendor-neutral deterministic generation contracts for cited advice."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from odp_api.ports.retrieval import RetrievedChunk

AdviceConfidence = Literal["HIGH", "MEDIUM", "LOW"]


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    defect_class: str
    confidence: AdviceConfidence
    retrieved_chunks: Sequence[RetrievedChunk]


@dataclass(frozen=True, slots=True)
class GeneratedCitation:
    source_name: str
    document_version: int
    page_number: int
    paragraph_number: int
    snippet: str


@dataclass(frozen=True, slots=True)
class GeneratedAdvice:
    answer: str
    citations: tuple[GeneratedCitation, ...]


class LLMGenerationPort(Protocol):
    """Converts retrieved evidence into displayable advice without side effects."""

    def generate(self, request: GenerationRequest) -> GeneratedAdvice: ...
