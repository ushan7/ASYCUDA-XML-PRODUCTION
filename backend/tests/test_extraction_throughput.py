"""Throughput work: scoped repair, the reserved slot, packing window size.

The packing-list budget was being spent on two things that are not extraction:
queueing behind undeadlined work on a global gate, and re-sending an entire
window's rows to repair one page that came back empty.
"""
import json
import threading

import pytest

from app.domain.enums import DeclaredRole
from app.extraction import openai_extractor as oe
from app.extraction.openai_extractor import OpenAIExtractor
from app.ocr.base import OcrDocument, OcrPage

ROLE_OK = {"expected_role": "INVOICE", "matches_expected_role": True}
PAGE = "|  MODEL | DESC | 1 | EA | 100.00 | 100.00  |"


def _row(page, idx, desc):
    return {"source_page_no": page, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": "100.00",
            "line_total_raw": "100.00"}


class _Routed:
    def __init__(self, routes):
        self._routes = routes
        self.calls = 0
        self.prompts = []

    def create(self, **kw):
        self.calls += 1
        user = " ".join(m["content"] for m in kw["messages"] if m["role"] == "user")
        self.prompts.append(user)
        for key, content in self._routes.items():
            if key in user:
                msg = type("M", (), {"content": content})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()
        raise AssertionError("no route matched: " + user[-200:])


class _Client:
    def __init__(self, routes):
        self.chat = type("Chat", (), {"completions": _Routed(routes)})()


@pytest.fixture
def _single_window(monkeypatch):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.deterministic_table_parser_enabled = False
    s.extraction_chunk_page_threshold = 10          # one window over both pages
    s.llm_concurrency = 2
    s.openai_reasoning_fallback_model = None
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    monkeypatch.setattr(oe, "_LLM_GATE", None)
    return s


def test_empty_page_is_repaired_by_a_scoped_call_not_a_full_resend(_single_window):
    """The first response drops page 2 entirely.  The repair asks for page 2
    ONLY — the whole-document resend would re-emit page 1's rows as well, which
    on a long packing list is the difference between a repair and a timeout."""
    first = json.dumps({"role_validation": ROLE_OK, "page_numbers": [1, 2],
                        "rows": [_row(1, 1, "A1")]})
    gap = json.dumps({"role_validation": ROLE_OK, "rows": [_row(2, 1, "A2")]})
    client = _Client({"ONLY page(s) [2]": gap, "<PAGE 1>": first})
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                      pages=[OcrPage(page_no=i, plain_text=PAGE) for i in (1, 2)])

    payload, warnings = OpenAIExtractor(client=client).extract(DeclaredRole.INVOICE, ocr)
    assert [r.description_raw for r in payload.rows] == ["A1", "A2"]
    assert client.chat.completions.calls == 2
    assert any("GAP_FILLED" in w for w in warnings)
    # the repair prompt asked for page 2 and did not demand the whole document
    repair = client.chat.completions.prompts[-1]
    assert "ONLY page(s) [2]" in repair
    assert "resend the COMPLETE corrected JSON" not in repair


def test_gap_fill_pages_declines_when_any_other_error_is_present():
    """A scoped repair is only safe for pages that returned NOTHING.  Any other
    error means the window itself has to be re-stated."""
    payload = type("P", (), {"rows": []})()
    assert oe._gap_fill_pages(["PAGE_ROWS_MISSING: page 2 prints goods-table rows"], payload) == {2}
    assert oe._gap_fill_pages(
        ["PAGE_ROWS_MISSING: page 2 prints goods-table rows",
         "row 3: quantity not numeric"], payload) == set()


def test_gap_fill_never_targets_a_page_that_already_returned_rows():
    """Appending to a page that DID contribute rows risks duplicating a real
    customs line — the failure automatic dedup was withdrawn to avoid."""
    payload = type("P", (), {"rows": [type("R", (), {"source_page_no": 2})()]})()
    assert oe._gap_fill_pages(["PAGE_ROWS_MISSING: page 2 prints goods-table rows"],
                              payload) == set()


def test_anchor_missing_is_not_scope_repairable():
    payload = type("P", (), {"rows": []})()
    assert oe._gap_fill_pages(["ROW_ANCHOR_MISSING: page 2 prints goods row ..."], payload) == set()


# --------------------------------------------------------------------------- #
# The reserved slot
# --------------------------------------------------------------------------- #
def test_a_deadlined_call_gets_a_slot_even_when_the_shared_gate_is_full(monkeypatch):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.llm_concurrency = 1
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    monkeypatch.setattr(oe, "_LLM_GATE", None)
    gate = oe._llm_gate()
    gate.acquire()                                   # undeadlined work holds the only slot
    try:
        entered = threading.Event()

        def _run(priority):
            with oe._llm_slot(priority=priority):
                entered.set()

        t = threading.Thread(target=_run, args=(True,), daemon=True)
        t.start()
        assert entered.wait(2.0), "a deadlined call was starved by the shared gate"
        t.join(2.0)

        blocked = threading.Event()
        t2 = threading.Thread(target=lambda: (oe._llm_slot(False).__enter__(), blocked.set()),
                              daemon=True)
        t2.start()
        assert not blocked.wait(0.4), "an undeadlined call must still queue"
    finally:
        gate.release()
        monkeypatch.setattr(oe, "_LLM_GATE", None)


def test_gate_sizing_is_unchanged_by_the_reserved_slot(monkeypatch):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.llm_concurrency = 3
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    monkeypatch.setattr(oe, "_LLM_GATE", None)
    assert oe._llm_gate()._value == 3
    monkeypatch.setattr(oe, "_LLM_GATE", None)


# --------------------------------------------------------------------------- #
# Window size
# --------------------------------------------------------------------------- #
def test_packing_lists_use_the_smaller_window():
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    assert oe._window_size(DeclaredRole.PACKING_LIST, s) == s.extraction_chunk_page_size_packing
    assert oe._window_size(DeclaredRole.INVOICE, s) == s.extraction_chunk_page_size
    assert s.extraction_chunk_page_size_packing < s.extraction_chunk_page_size
