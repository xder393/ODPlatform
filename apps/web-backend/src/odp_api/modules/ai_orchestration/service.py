"""Tenant-scoped, confidence-tiered advice orchestration."""

import re
from typing import Literal

from odp_api.modules.inspection.models import DefectCase
from odp_api.ports.generation import GenerationRequest, LLMGenerationPort
from odp_api.ports.retrieval import RAGRetrievalPort, RetrievalFilters, RetrievedChunk
from pydantic import BaseModel

HUMAN_REVIEW_MESSAGE = "AI advice unavailable. Please use human review."
AdviceConfidence = Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"]


class Citation(BaseModel):
    source_name: str
    document_version: int
    page_number: int
    paragraph_number: int
    snippet: str


class AdviceResponse(BaseModel):
    answer: str
    citations: list[Citation]
    confidence: AdviceConfidence


class AdviceService:
    """Chooses the strongest safely scoped evidence tier for one defect case."""

    def __init__(self, retrieval: RAGRetrievalPort, generator: LLMGenerationPort) -> None:
        self._retrieval = retrieval
        self._generator = generator

    def advise(self, defect_case: DefectCase) -> AdviceResponse:
        defect_class = defect_case.inspection_events[0].defect_class
        product_category = _product_category(defect_case, defect_class)
        query = f"{defect_class} inspection guidance"
        evidence_tiers: tuple[tuple[Literal["HIGH", "MEDIUM", "LOW"], RetrievalFilters], ...] = (
            (
                "HIGH",
                RetrievalFilters(
                    line_id=defect_case.line_id,
                    product_category=product_category,
                ),
            ),
            ("MEDIUM", RetrievalFilters(line_id=defect_case.line_id)),
            ("LOW", RetrievalFilters()),
        )
        for confidence, filters in evidence_tiers:
            chunks = self._retrieval.search(query, defect_case.organization_id, filters)
            if chunks:
                return self._render(defect_class, confidence, chunks)
        return AdviceResponse(
            answer=HUMAN_REVIEW_MESSAGE,
            citations=[],
            confidence="UNAVAILABLE",
        )

    def _render(
        self,
        defect_class: str,
        confidence: Literal["HIGH", "MEDIUM", "LOW"],
        chunks: list[RetrievedChunk],
    ) -> AdviceResponse:
        generated = self._generator.generate(
            GenerationRequest(
                defect_class=defect_class,
                confidence=confidence,
                retrieved_chunks=chunks,
            )
        )
        answer = generated.answer
        if confidence == "LOW":
            answer = _remove_pause_recommendations(answer)
        return AdviceResponse(
            answer=answer,
            citations=[
                Citation(
                    source_name=citation.source_name,
                    document_version=citation.document_version,
                    page_number=citation.page_number,
                    paragraph_number=citation.paragraph_number,
                    snippet=citation.snippet,
                )
                for citation in generated.citations
            ],
            confidence=confidence,
        )


def _product_category(defect_case: DefectCase, default: str) -> str:
    """Read optional product context without changing the immutable case contract."""
    metadata = dict(defect_case.inspection_events[0].preprocessing_parameters)
    return metadata.get("product_category", default)


def _remove_pause_recommendations(answer: str) -> str:
    """Defence in depth for low-confidence generations from any future adapter."""
    sanitized = re.sub(
        r"(?i)\b(?:pause|stop)\b[^.!?]*[.!?]?",
        "Request human review.",
        answer,
    )
    return sanitized or "Request human review."
