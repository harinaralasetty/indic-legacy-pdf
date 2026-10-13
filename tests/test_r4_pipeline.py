"""Public API compatibility regressions."""

import fitz
import pytest

from legacylipi.api.pipeline import run_translate
from legacylipi.api.schemas import TranslateRequest


@pytest.mark.asyncio
async def test_r4_translate_result_is_two_item_tuple():
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 70), "Hello world")
        data = pdf.tobytes()
    result = await run_translate(
        data, "input.pdf", TranslateRequest(translator="mock", output_format="text")
    )
    assert result[0] == result.content
    assert result[1] == result.filename
    assert len(result) == 2
    content, filename = result
    assert isinstance(result, tuple)
    assert (content, filename) == result
    assert result.warnings == []
