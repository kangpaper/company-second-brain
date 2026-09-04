from company_brain.knowledge.embeddings import DeterministicEmbeddingProvider, embed_chunks
from company_brain.knowledge.markdown import chunk_markdown, parse_markdown


def test_markdown_chunks_preserve_heading_path_and_stable_hash() -> None:
    markdown = """# Customer ABC

Overview paragraph.

## Payment history

Three invoices are overdue.
"""

    chunks = chunk_markdown(markdown)

    assert [chunk.heading_path for chunk in chunks] == [
        ["Customer ABC"],
        ["Customer ABC", "Payment history"],
    ]
    assert chunks[1].text == "Three invoices are overdue."
    assert len(chunks[1].content_hash) == 64
    assert chunk_markdown(markdown)[1].content_hash == chunks[1].content_hash


def test_embedding_pipeline_depends_on_provider_contract() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=8)
    chunks = chunk_markdown("# Review\n\nRevenue declined.")

    records = embed_chunks(chunks, provider)

    assert records[0].provider == "deterministic-test"
    assert records[0].model == "sha256-v1"
    assert len(records[0].vector) == 8
    assert records == embed_chunks(chunks, provider)


def test_frontmatter_rejects_non_json_yaml_values() -> None:
    import pytest

    with pytest.raises(ValueError, match="JSON-compatible"):
        parse_markdown("---\ntitle: Review\nreviewed_at: 2026-08-12\n---\nBody")


def test_frontmatter_rejects_non_finite_numbers() -> None:
    import pytest

    for value in (".nan", ".inf", "-.inf"):
        with pytest.raises(ValueError, match="JSON-compatible"):
            parse_markdown(f"---\ntitle: Review\nscore: {value}\n---\nBody")
