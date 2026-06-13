"""Tests for api/citation_stripping.strip_unverified_claims."""
from api.citation_stripping import strip_unverified_claims


def test_drops_block_whose_citations_are_all_broken():
    content = (
        "## Heading\n\n"
        "Real claim. Sources: [a.py:1-2]()\n\n"
        "Fabricated claim. Sources: [ghost.py:9-9]()"
    )
    citations = {
        "a.py:1-2": {"status": "verified"},
        "ghost.py:9-9": {"status": "broken"},
    }
    out = strip_unverified_claims(content, citations)
    assert "Fabricated claim" not in out
    assert "ghost.py" not in out
    assert "Real claim" in out
    assert "## Heading" in out


def test_keeps_block_with_one_verified_citation():
    content = "Mixed claim. Sources: [a.py:1-2]() [ghost.py:9-9]()"
    citations = {
        "a.py:1-2": {"status": "verified"},
        "ghost.py:9-9": {"status": "broken"},
    }
    out = strip_unverified_claims(content, citations)
    assert "Mixed claim" in out
    assert "ghost.py:9-9" in out


def test_keeps_block_with_no_citations():
    content = "Just prose, no citations here."
    assert strip_unverified_claims(content, {}) == "Just prose, no citations here."


def test_merges_standalone_sources_block_into_claim():
    # Claim and its Sources line separated by a blank line: must drop together.
    content = "Fabricated claim on its own line.\n\nSources: [ghost.py:9-9]()"
    citations = {"ghost.py:9-9": {"status": "broken"}}
    out = strip_unverified_claims(content, citations)
    assert out.strip() == ""


def test_ignores_non_citation_empty_links():
    # An empty link without a file extension is not a citation -> not a trigger.
    content = "See [the docs](). Sources: [a.py:1-2]()"
    citations = {"a.py:1-2": {"status": "verified"}}
    out = strip_unverified_claims(content, citations)
    assert "See" in out
