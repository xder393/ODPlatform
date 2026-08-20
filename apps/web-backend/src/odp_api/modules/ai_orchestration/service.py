"""Tenant-scoped, confidence-tiered advice orchestration."""

from typing import Literal
from uuid import UUID

from odp_api.modules.inspection.models import DefectCase
from odp_api.ports.generation import GenerationRequest, LLMGenerationPort
from odp_api.ports.retrieval import RAGRetrievalPort, RetrievalFilters, RetrievedChunk
from pydantic import BaseModel

HUMAN_REVIEW_MESSAGE = "AI advice unavailable. Please use human review."
LOW_CONFIDENCE_ANSWER = (
    "Only cross-product evidence is available. Request human review before disposition."
)
AdviceConfidence = Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"]


class Citation(BaseModel):
    chunk_id: UUID
    document_id: UUID
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
        product_category = defect_case.product_category
        query = f"{defect_class} inspection guidance"
        evidence_tiers = _evidence_tiers(defect_case.line_id, product_category)
        for confidence, filters in evidence_tiers:
            chunks = self._retrieval.search(query, defect_case.organization_id, filters)
            applicable_chunks = [
                chunk
                for chunk in chunks
                if _is_applicable(
                    chunk,
                    confidence,
                    defect_case.organization_id,
                    defect_case.line_id,
                    product_category,
                )
            ]
            if applicable_chunks:
                return self._render(defect_class, confidence, applicable_chunks)
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
        ordered_chunks = sorted(
            chunks,
            key=lambda chunk: (
                -chunk.score,
                chunk.source_name,
                chunk.document_version,
                chunk.page_number,
                chunk.paragraph_number,
                str(chunk.chunk_id),
            ),
        )
        if confidence == "LOW":
            return AdviceResponse(
                answer=LOW_CONFIDENCE_ANSWER,
                citations=_citations(ordered_chunks),
                confidence=confidence,
            )
        generated = self._generator.generate(
            GenerationRequest(
                defect_class=defect_class,
                confidence=confidence,
                retrieved_chunks=ordered_chunks,
            )
        )
        return AdviceResponse(
            answer=generated.answer,
            citations=_citations(ordered_chunks),
            confidence=confidence,
        )


def _evidence_tiers(
    line_id: UUID | None,
    product_category: str | None,
) -> tuple[tuple[Literal["HIGH", "MEDIUM", "LOW"], RetrievalFilters], ...]:
    if product_category is None:
        return ()
    tiers: list[tuple[Literal["HIGH", "MEDIUM", "LOW"], RetrievalFilters]] = []
    if line_id is not None:
        for confidence, evidence_kind in (
            ("HIGH", "CURRENT_SPECIFICATION"),
            ("MEDIUM", "HISTORICAL_CASE"),
        ):
            tiers.append(
                (
                    confidence,
                    RetrievalFilters(
                        evidence_kind=evidence_kind,
                        line_id=line_id,
                        product_category=product_category,
                        require_exact_line_scope=True,
                        require_exact_product_scope=True,
                    ),
                )
            )
    tiers.append(
        (
            "LOW",
            RetrievalFilters(
                require_evidence_kind=True,
                exclude_product_category=product_category,
            ),
        )
    )
    return tuple(tiers)


def _is_applicable(
    chunk: RetrievedChunk,
    confidence: Literal["HIGH", "MEDIUM", "LOW"],
    organization_id: UUID,
    line_id: UUID | None,
    product_category: str | None,
) -> bool:
    if chunk.organization_id != organization_id or product_category is None:
        return False
    if confidence == "HIGH":
        return (
            chunk.evidence_kind == "CURRENT_SPECIFICATION"
            and chunk.applicable_line_id == line_id
            and chunk.product_category == product_category
        )
    if confidence == "MEDIUM":
        return (
            chunk.evidence_kind == "HISTORICAL_CASE"
            and chunk.applicable_line_id == line_id
            and chunk.product_category == product_category
        )
    return (
        chunk.evidence_kind is not None
        and chunk.product_category is not None
        and chunk.product_category != product_category
    )


def _citations(chunks: list[RetrievedChunk]) -> list[Citation]:
    return [
        Citation(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            source_name=chunk.source_name,
            document_version=chunk.document_version,
            page_number=chunk.page_number,
            paragraph_number=chunk.paragraph_number,
            snippet=chunk.text,
        )
        for chunk in chunks
    ]
