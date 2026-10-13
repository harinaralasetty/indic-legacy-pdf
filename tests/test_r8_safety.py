"""Self-contained R7 cache and output-path regressions, including failure injection."""

import io
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner
from fontTools.ttLib import TTFont

from legacylipi.cli import main
from legacylipi.core import glyph_recovery as gr
from tests.glyph_helpers import FONT, make_pdf


def test_font_cache_160_variants_16_threads_five_repetitions():
    programs = []
    for i in range(160):
        with TTFont(FONT) as font:
            font["name"].setName(f"R8 cache stress {i}", 1, 3, 1, 0x409)
            out = io.BytesIO()
            font.save(out)
            programs.append(out.getvalue())
    interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(1e-6)
        for repetition in range(5):
            with gr._TELUGU_FONT_CACHE_LOCK:
                gr._TELUGU_FONT_CACHE.clear()
            for data in programs[:128]:
                assert gr._is_telugu_font(data)

            def inspect(worker, repetition=repetition):
                order = list(programs)
                random.Random(repetition * 16 + worker).shuffle(order)
                for data in order:
                    assert gr._is_telugu_font(data)
                    with gr._TELUGU_FONT_CACHE_LOCK:
                        assert len(gr._TELUGU_FONT_CACHE) <= gr.FONT_VERDICT_CACHE_LIMIT

            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(inspect, range(16)))
    finally:
        sys.setswitchinterval(interval)


def test_invalid_report_preserves_output(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    output = tmp_path / "out.txt"
    output.write_bytes(b"ORIGINAL OUTPUT")

    def unexpected_recovery(*args, **kwargs):
        pytest.fail("Both destinations must be validated before recovery")

    monkeypatch.setattr(gr, "recover_pdf", unexpected_recovery)
    result = CliRunner().invoke(
        main,
        [
            "recover-text",
            str(source),
            "-o",
            str(output),
            "--report",
            str(tmp_path / "absent" / "report.json"),
        ],
    )
    assert result.exit_code == 1
    assert "Destination directory does not exist" in result.output
    assert output.read_bytes() == b"ORIGINAL OUTPUT"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt", "source.pdf"]


@pytest.mark.parametrize("failure", ["stage", "fsync", "backup", "first_replace", "second_replace"])
@pytest.mark.parametrize("preexisting", [False, True])
def test_output_transaction_failure(tmp_path, monkeypatch, failure, preexisting):
    import tempfile

    source = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    output = tmp_path / "out.txt"
    report = tmp_path / "report.json"
    if preexisting:
        output.write_bytes(b"ORIGINAL TEXT")
        report.write_bytes(b"ORIGINAL REPORT")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    if failure in ("first_replace", "second_replace"):
        original = os.replace
        count = 0

        def fail_replace(src, dst):
            nonlocal count
            count += 1
            if count == (1 if failure == "first_replace" else 2):
                raise OSError("injected replace failure")
            return original(src, dst)

        monkeypatch.setattr(os, "replace", fail_replace)
    else:
        module, name = (
            (tempfile, "NamedTemporaryFile")
            if failure == "stage"
            else (os, "fsync" if failure == "fsync" else "link")
        )
        if failure == "backup" and not preexisting:
            # No originals means no backup operation; inject staging failure.
            module, name = tempfile, "NamedTemporaryFile"
        original = getattr(module, name)
        count = 0

        def fail_stage(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected staging failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, fail_stage)
    result = CliRunner().invoke(
        main, ["recover-text", str(source), "-o", str(output), "--report", str(report)]
    )
    assert result.exit_code == 1, result.output
    assert "injected" in result.output
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_output_transaction_success_across_directories(tmp_path):
    import json

    source = make_pdf(tmp_path / "source.pdf", lines=["తెలుగు"])
    directory = tmp_path / "reports"
    directory.mkdir()
    output, report = tmp_path / "out.txt", directory / "report.json"
    output.write_text("old text")
    report.write_text("old report")
    result = CliRunner().invoke(
        main, ["recover-text", str(source), "-o", str(output), "--report", str(report)]
    )
    assert result.exit_code == 0, result.output
    assert output.read_text().strip() == "తెలుగు"
    assert json.loads(report.read_text())["unknown_count"] == 0
    assert list(directory.iterdir()) == [report]
