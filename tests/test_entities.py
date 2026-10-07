from lending_risk_mcp.entities import normalize, similarity
from lending_risk_mcp.sources import HmdaSource
from lending_risk_mcp.sources.hmda import FILERS

from .conftest import WF_LEI, hmda_filers_body


def test_normalize_strips_legal_suffixes():
    assert normalize("Wells Fargo Bank, National Association") == "wells fargo bank"
    assert normalize("WELLS FARGO & COMPANY/MN") == "wells fargo"
    assert normalize("Rocket Mortgage, LLC") == "rocket mortgage"


def test_similarity_bank_vs_holding_company():
    assert similarity("Wells Fargo", "WELLS FARGO & COMPANY") == 1.0
    assert similarity("Wells Fargo", "Fargo Federal Credit Union") < 0.6


def test_registry_match_alias(registry):
    entry, score, _ = registry.match("quicken loans")
    assert entry["id"] == "rocket" and score == 1.0


def test_registry_no_match(registry):
    entry, _score, alts = registry.match("Some Tiny Credit Union")
    assert entry is None and len(alts) == 3


async def test_resolve_fills_lei_at_runtime(registry, mock_api):
    mock_api.get(FILERS.format(year=2023)).respond(json=hmda_filers_body())
    lender = await registry.resolve("Wells Fargo", hmda=HmdaSource(retries=0))
    assert lender.lei.value == WF_LEI
    assert lender.lei.method == "runtime_lookup"
    assert lender.cik.value == 72971 and lender.cik.method == "registry"


async def test_resolve_survives_lookup_failure(registry, mock_api):
    mock_api.get(FILERS.format(year=2023)).respond(500)
    lender = await registry.resolve("Wells Fargo", hmda=HmdaSource(retries=0))
    assert lender.lei.value is None and lender.lei.method == "unresolved"
    assert lender.cik.value == 72971  # the rest still works
