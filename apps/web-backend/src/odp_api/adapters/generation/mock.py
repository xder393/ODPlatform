"""Deterministic development-only renderer for retrieved inspection evidence."""

from odp_api.ports.generation import (
    GeneratedAdvice,
    GeneratedCitation,
    GenerationRequest,
)


class MockLLMAdapter:
    """Pure template renderer: equal evidence always produces equal advice."""

    def generate(self, request: GenerationRequest) -> GeneratedAdvice:
        chunks = tuple(
            sorted(
                request.retrieved_chunks,
                key=lambda chunk: (
                    -chunk.score,
                    chunk.source_name,
                    chunk.document_version,
                    chunk.page_number,
                    chunk.paragraph_number,
                    str(chunk.chunk_id),
                ),
            )
        )
        citations = tuple(
            GeneratedCitation(
                source_name=chunk.source_name,
                document_version=chunk.document_version,
                page_number=chunk.page_number,
                paragraph_number=chunk.paragraph_number,
                snippet=chunk.text,
            )
            for chunk in chunks
        )
        primary_snippet = citations[0].snippet
        return GeneratedAdvice(
            answer=_answer(request.defect_class, request.confidence, primary_snippet),
            citations=citations,
        )


def _answer(defect_class: str, confidence: str, snippet: str) -> str:
    if confidence == "HIGH":
        return (
            f"Detected {defect_class}. Follow the current product and line specification: "
            f'"{snippet}"'
        )
    if confidence == "MEDIUM":
        return (
            f"Detected {defect_class}. A same-line historical case is relevant: "
            f'"{snippet}" Review it with the quality lead before disposition.'
        )
    return (
        f"Detected {defect_class}. Cross-product reference only: "
        f'"{snippet}" Request human review before disposition.'
    )
