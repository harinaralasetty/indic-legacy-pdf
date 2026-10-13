"""Command Line Interface for LegacyLipi.

This module provides the CLI commands for the LegacyLipi tool.
"""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from legacylipi import __version__
from legacylipi.core.encoding_detector import EncodingDetector
from legacylipi.core.models import OutputFormat
from legacylipi.core.ocr_parser import (
    OCRError,
    check_language_available,
    check_tesseract_available,
    get_available_languages,
    parse_pdf_with_ocr,
)
from legacylipi.core.output_generator import OutputGenerator
from legacylipi.core.pdf_parser import PDFParseError, parse_pdf
from legacylipi.core.translator import (
    TranslationError,
    UsageLimitExceededError,
    create_translator,
)
from legacylipi.core.unicode_converter import UnicodeConverter
from legacylipi.mappings.loader import MappingLoader

# Initialize Rich console
console = Console()


def print_banner():
    """Print the LegacyLipi banner."""
    console.print(f"\n[bold blue]LegacyLipi[/bold blue] v{__version__}", style="bold")
    console.print("[dim]Legacy Font PDF Translator[/dim]")
    console.print("─" * 50)


def print_error(message: str):
    """Print an error message."""
    console.print(f"\n[bold red]❌ Error:[/bold red] {message}")


def print_success(message: str):
    """Print a success message."""
    console.print(f"\n[bold green]✓[/bold green] {message}")


def print_warning(message: str):
    """Print a warning message."""
    console.print(f"[bold yellow]⚠[/bold yellow] {message}")


@click.group()
@click.version_option(version=__version__, prog_name="LegacyLipi")
def main():
    """LegacyLipi - Legacy Font PDF Translator.

    Translate PDF documents with legacy Indian font encodings to English.
    """
    pass


@main.command(deprecated=True)
def ui():
    """[Removed] Use 'legacylipi api' instead."""
    raise click.ClickException(
        "The NiceGUI-based 'ui' command has been removed.\n"
        "Use 'legacylipi api' to launch the new React web interface."
    )


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path. Defaults to input file with new extension.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "markdown", "md", "pdf"]),
    default="text",
    help="Output format (text, markdown, or pdf).",
)
@click.option(
    "--encoding",
    type=str,
    default=None,
    help="Force specific encoding (e.g., shree-lipi, kruti-dev).",
)
@click.option("--source-lang", default=None, help="Source language code (default: from encoding).")
@click.option(
    "--target-lang",
    type=str,
    default="en",
    help="Target language code (default: en for English).",
)
@click.option(
    "--translator",
    type=click.Choice(["mock", "google", "trans", "mymemory", "ollama", "openai", "gcp_cloud"]),
    default="google",
    help="Translation backend: google, trans, mymemory, ollama, openai, gcp_cloud (requires GCP project).",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Model to use for Ollama backend.",
)
@click.option(
    "--trans-path",
    type=str,
    default=None,
    help="Path to trans executable (for --translator trans). Default: searches PATH and ./trans",
)
@click.option(
    "--gcp-project",
    type=str,
    envvar="GCP_PROJECT_ID",
    default=None,
    help="GCP project ID for Cloud Translation API. Can also set GCP_PROJECT_ID env var.",
)
@click.option(
    "--force-translate",
    is_flag=True,
    help="Continue translation even if GCP free tier limit would be exceeded.",
)
@click.option(
    "--no-translate",
    is_flag=True,
    help="Skip translation, only convert to Unicode.",
)
@click.option(
    "--ocr-only",
    is_flag=True,
    help="OCR extraction only, no translation (implies --use-ocr --no-translate).",
)
@click.option(
    "--use-ocr",
    is_flag=True,
    help="Use OCR to extract text from PDF (useful for scanned documents or legacy fonts).",
)
@click.option(
    "--ocr-lang",
    type=str,
    default="mar",
    help="OCR language code (e.g., 'mar' for Marathi, 'hin' for Hindi). Default: mar",
)
@click.option(
    "--ocr-dpi",
    type=int,
    default=300,
    help="DPI for OCR rendering (higher = better quality but slower). Default: 300",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress progress output.",
)
@click.option(
    "--structure-preserving",
    is_flag=True,
    help="Preserve original text positions in PDF output (requires positioned text blocks).",
)
@click.option(
    "--bilingual",
    is_flag=True,
    help="Generate bilingual side-by-side output (Markdown format, ignores --format).",
)
@click.option(
    "--strict-cmap", is_flag=True, help="Refuse recovery that uses inferred cmap mappings."
)
def translate(
    strict_cmap: bool,
    input_file: Path,
    output: Path | None,
    output_format: str,
    encoding: str | None,
    source_lang: str | None,
    target_lang: str,
    translator: str,
    model: str | None,
    trans_path: str | None,
    gcp_project: str | None,
    force_translate: bool,
    no_translate: bool,
    ocr_only: bool,
    use_ocr: bool,
    ocr_lang: str,
    ocr_dpi: int,
    quiet: bool,
    structure_preserving: bool,
    bilingual: bool,
):
    """Translate a PDF with legacy fonts to Unicode/English.

    This command performs the full translation pipeline:
    1. Parse PDF and extract text (or use OCR with --use-ocr)
    2. Detect font encoding (skipped in OCR mode)
    3. Convert legacy encoding to Unicode (skipped in OCR mode)
    4. Translate to target language
    5. Output to specified format
    """
    # Handle --ocr-only convenience flag
    if ocr_only:
        use_ocr = True
        no_translate = True

    if not quiet:
        print_banner()

    # Validate OCR requirements if using OCR mode
    if use_ocr:
        available, msg = check_tesseract_available()
        if not available:
            print_error(msg)
            console.print("\n[dim]To install Tesseract on Ubuntu/Debian:[/dim]")
            console.print("  sudo apt-get install tesseract-ocr tesseract-ocr-mar")
            console.print("\n[dim]On macOS:[/dim]")
            console.print("  brew install tesseract tesseract-lang")
            sys.exit(1)

        if not check_language_available(ocr_lang):
            print_error(f"OCR language '{ocr_lang}' is not available.")
            available_langs = get_available_languages()
            if available_langs:
                console.print(
                    f"\n[dim]Available languages:[/dim] {', '.join(available_langs[:10])}"
                )
            console.print("\n[dim]To install Marathi language pack:[/dim]")
            console.print("  sudo apt-get install tesseract-ocr-mar")
            sys.exit(1)

    # Determine output format
    if output_format == "pdf":
        fmt = OutputFormat.PDF
        default_ext = ".pdf"
    elif output_format in ("markdown", "md"):
        fmt = OutputFormat.MARKDOWN
        default_ext = ".md"
    else:
        fmt = OutputFormat.TEXT
        default_ext = ".txt"

    # Determine output path
    if output is None:
        output = input_file.with_suffix(default_ext)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=quiet,
        ) as progress:
            # Step 1: Parse PDF (with OCR or standard text extraction)
            if use_ocr:
                task = progress.add_task(f"Running OCR ({ocr_lang})...", total=None)
                document = parse_pdf_with_ocr(input_file, lang=ocr_lang, dpi=ocr_dpi)
                progress.update(
                    task, description=f"[green]✓[/green] OCR extracted {document.page_count} pages"
                )

                if not quiet:
                    console.print(
                        f"\n[dim]📄 Input:[/dim] {input_file.name} ({document.page_count} pages)"
                    )
                    console.print(f"[dim]👁 OCR:[/dim] {ocr_lang} @ {ocr_dpi} DPI")

                # In OCR mode, text is already Unicode - create dummy encoding result
                from legacylipi.core.models import DetectionMethod, EncodingDetectionResult

                encoding_result = EncodingDetectionResult(
                    detected_encoding="unicode-ocr",
                    confidence=1.0,
                    method=DetectionMethod.UNICODE_DETECTED,
                )
                # OCR output is already Unicode, no conversion needed
                converted_doc = document
            else:
                task = progress.add_task("Parsing PDF...", total=None)
                document = parse_pdf(input_file)
                progress.update(
                    task, description=f"[green]✓[/green] Parsed {document.page_count} pages"
                )

                if not quiet:
                    console.print(
                        f"\n[dim]📄 Input:[/dim] {input_file.name} ({document.page_count} pages)"
                    )

                # Step 2: Detect encoding
                progress.update(task, description="Detecting encoding...")
                detector = EncodingDetector()

                if encoding:
                    # User specified encoding
                    from legacylipi.core.models import DetectionMethod, EncodingDetectionResult

                    encoding_result = EncodingDetectionResult(
                        detected_encoding=encoding,
                        confidence=1.0,
                        method=DetectionMethod.USER_OVERRIDE,
                    )
                    page_encodings = {page.page_number: encoding_result for page in document.pages}
                else:
                    encoding_result, page_encodings = detector.detect_from_document(document)

                progress.update(
                    task,
                    description=f"[green]✓[/green] Encoding: {encoding_result.detected_encoding} ({encoding_result.confidence:.0%})",
                )

                if not quiet:
                    console.print(
                        f"[dim]🎯 Encoding:[/dim] {encoding_result.detected_encoding} (confidence: {encoding_result.confidence:.0%})"
                    )

                # Step 3: Convert to Unicode
                progress.update(task, description="Converting to Unicode...")
                converter = UnicodeConverter()
                converted_doc = converter.convert_with_recovery(
                    document,
                    encoding_result,
                    page_encodings=page_encodings,
                    strict_cmap=strict_cmap,
                )
                _show_recovery_warnings(converted_doc.warnings)
                progress.update(task, description="[green]✓[/green] Converted to Unicode")

            # Step 4: Translate (if enabled)
            translation_result = None
            if not no_translate:
                progress.update(task, description=f"Translating with {translator}...")

                translator_kwargs = {}
                if model and translator in ("ollama", "openai"):
                    translator_kwargs["model"] = model
                if trans_path and translator == "trans":
                    translator_kwargs["trans_path"] = trans_path
                if translator == "gcp_cloud":
                    if not gcp_project:
                        print_error(
                            "GCP project ID required for gcp_cloud translator. Use --gcp-project or set GCP_PROJECT_ID."
                        )
                        sys.exit(1)
                    translator_kwargs["project_id"] = gcp_project
                    translator_kwargs["enforce_free_tier"] = not force_translate

                engine = create_translator(translator, **translator_kwargs)

                # Translate each page individually to avoid page marker corruption
                from legacylipi.core.language_utils import get_source_language

                page_texts = [page.unicode_text for page in converted_doc.pages]
                detected_encoding = encoding_result.detected_encoding
                source_lang_code = source_lang or get_source_language(detected_encoding)

                import asyncio

                if structure_preserving and fmt == OutputFormat.PDF and not bilingual:
                    blocks = [
                        block
                        for page in converted_doc.pages
                        for block in page.text_blocks
                        if block.position is not None
                    ]
                    if blocks:
                        asyncio.run(
                            engine.translate_blocks_async(
                                blocks,
                                source_lang=source_lang_code,
                                target_lang=target_lang,
                            )
                        )
                        for block in blocks:
                            if block.translation_error:
                                console.print(
                                    f"[yellow]Warning: block translation failed: {block.translation_error}; source text retained.[/yellow]"
                                )
                    else:
                        structure_preserving = False
                else:
                    structure_preserving = False

                if not structure_preserving:
                    translated_pages = asyncio.run(
                        engine.translate_pages_async(
                            page_texts,
                            source_lang=source_lang_code,
                            target_lang=target_lang,
                        )
                    )

                    # Reassemble with page markers for output generation
                    text_parts = []
                    for i, translated_page in enumerate(translated_pages, 1):
                        text_parts.append(f"--- Page {i} ---\n{translated_page}")
                    translated_text = "\n\n".join(text_parts)

                    # Create TranslationResult from the per-page translations
                    from legacylipi.core.models import TranslationResult

                    unicode_text = "\n\n".join(
                        f"--- Page {i} ---\n{page.unicode_text}"
                        for i, page in enumerate(converted_doc.pages, 1)
                    )
                    translation_result = TranslationResult(
                        source_text=unicode_text,
                        translated_text=translated_text,
                        source_language=source_lang_code,
                        target_language=target_lang,
                        translation_backend=engine.backend_type,
                        chunk_count=len(translated_pages),
                    )
                # Check if translation actually happened (warnings indicate failures)
                if translation_result and translation_result.warnings:
                    # Check if ALL chunks failed (no translation happened)
                    if len(translation_result.warnings) >= translation_result.chunk_count:
                        progress.update(task, description="[red]✗[/red] Translation failed")
                        raise TranslationError(
                            f"All translation chunks failed. First error: {translation_result.warnings[0]}"
                        )
                    else:
                        # Partial failure - show warnings
                        progress.update(
                            task,
                            description=f"[yellow]⚠[/yellow] Translated with {len(translation_result.warnings)} warnings",
                        )
                        if not quiet:
                            for warning in translation_result.warnings[:3]:  # Show first 3 warnings
                                console.print(f"[yellow]   ⚠ {warning}[/yellow]")
                            if len(translation_result.warnings) > 3:
                                console.print(
                                    f"[yellow]   ... and {len(translation_result.warnings) - 3} more warnings[/yellow]"
                                )
                else:
                    progress.update(
                        task,
                        description="[green]✓[/green] Translated",
                    )

                if not quiet:
                    console.print(
                        f"[dim]🌐 Translated:[/dim] {len(converted_doc.unicode_text)} chars → {target_lang}"
                    )

            # Step 5: Generate output
            progress.update(task, description="Generating output...")
            generator = OutputGenerator()

            if bilingual and translation_result:
                # Bilingual output always uses Markdown format
                output_content = generator.generate_bilingual(
                    converted_doc,
                    encoding_result,
                    translation_result,
                )
                # Override output path extension
                if output.suffix != ".md":
                    output = output.with_suffix(".md")
            elif structure_preserving and not no_translate and fmt == OutputFormat.PDF:
                output_content = generator.generate_pdf(
                    converted_doc,
                    encoding_result,
                    structure_preserving_translation=True,
                )
            else:
                output_content = generator.generate(
                    converted_doc,
                    encoding_result,
                    translation_result,
                    fmt,
                )

            _show_recovery_warnings(generator.warnings)

            # Save output
            generator.save(output_content, output)
            progress.update(task, description=f"[green]✓[/green] Saved to {output.name}")

        print_success(f"Output saved to: {output}")

        # Print summary if not quiet
        if not quiet:
            console.print("\n[bold]📊 Summary:[/bold]")
            console.print(f"   • Pages processed: {document.page_count}")
            console.print(f"   • Encoding: {encoding_result.detected_encoding}")
            if translation_result:
                console.print(
                    f"   • Translation: {translation_result.source_language} → {translation_result.target_language}"
                )
                console.print(f"   • Backend: {translation_result.translation_backend.value}")

    except PDFParseError as e:
        print_error(f"Failed to parse PDF: {e}")
        sys.exit(1)
    except OCRError as e:
        print_error(f"OCR failed: {e}")
        console.print("\n[dim]Suggestions:[/dim]")
        console.print("  • Ensure Tesseract is installed: sudo apt-get install tesseract-ocr")
        console.print("  • Install language pack: sudo apt-get install tesseract-ocr-mar")
        console.print("  • Try without OCR to use font-based extraction")
        sys.exit(1)
    except TranslationError as e:
        print_error(f"Translation failed: {e}")
        console.print("\n[dim]Suggestions:[/dim]")
        console.print(
            "  • Try [cyan]--translator trans[/cyan] for translate-shell CLI (apt install translate-shell)"
        )
        console.print("  • Try [cyan]--translator ollama[/cyan] for local LLM translation")
        console.print("  • Try [cyan]--translator mymemory[/cyan] for free MyMemory API")
        console.print(
            "  • Try [cyan]--no-translate[/cyan] to skip translation and output Unicode only"
        )
        sys.exit(1)
    except UsageLimitExceededError as e:
        print_warning(
            f"GCP free tier limit: {e.current_usage:,}/{e.limit:,} characters used this month."
        )
        print_warning(f"This translation requires {e.requested:,} more characters.")
        console.print("\n[dim]Options:[/dim]")
        console.print("  • Use [cyan]--force-translate[/cyan] to proceed (charges may apply)")
        console.print("  • Try [cyan]--translator google[/cyan] for free Google Translate")
        console.print("  • Try [cyan]--translator ollama[/cyan] for local LLM translation")
        sys.exit(1)
    except Exception as e:
        # Handle tenacity retry errors that wrap TranslationError
        error_msg = str(e)
        if (
            "TranslationError" in error_msg
            or "403" in error_msg
            or "429" in error_msg
            or "proxy" in error_msg.lower()
        ):
            print_error("Translation service unavailable (network may be blocking external APIs)")
            console.print("\n[dim]Suggestions:[/dim]")
            console.print(
                "  • Try [cyan]--translator trans[/cyan] for translate-shell CLI (apt install translate-shell)"
            )
            console.print("  • Try [cyan]--translator ollama[/cyan] for local LLM translation")
            console.print("  • Try [cyan]--translator mymemory[/cyan] for free MyMemory API")
            console.print(
                "  • Try [cyan]--no-translate[/cyan] to skip translation and output Unicode only"
            )
        else:
            print_error(str(e))
            if not quiet and not isinstance(e, ValueError):
                console.print_exception()
        sys.exit(1)


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path for Unicode text.",
)
@click.option(
    "--encoding",
    type=str,
    default=None,
    help="Force specific encoding.",
)
@click.option(
    "--strict-cmap", is_flag=True, help="Refuse recovery that uses inferred cmap mappings."
)
def convert(
    strict_cmap: bool,
    input_file: Path,
    output: Path | None,
    encoding: str | None,
):
    """Convert PDF from legacy encoding to Unicode (no translation).

    This command only performs encoding detection and Unicode conversion,
    without translating to another language.
    """
    print_banner()

    if output is None:
        output = input_file.with_suffix(".unicode.txt")

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing...", total=None)

            # Parse PDF
            document = parse_pdf(input_file)
            progress.update(task, description=f"Parsed {document.page_count} pages")

            # Detect encoding
            detector = EncodingDetector()
            if encoding:
                from legacylipi.core.models import DetectionMethod, EncodingDetectionResult

                encoding_result = EncodingDetectionResult(
                    detected_encoding=encoding,
                    confidence=1.0,
                    method=DetectionMethod.USER_OVERRIDE,
                )
                page_encodings = {page.page_number: encoding_result for page in document.pages}
            else:
                encoding_result, page_encodings = detector.detect_from_document(document)

            progress.update(task, description=f"Encoding: {encoding_result.detected_encoding}")

            # Convert to Unicode
            converter = UnicodeConverter()
            converted_doc = converter.convert_with_recovery(
                document, encoding_result, page_encodings, strict_cmap=strict_cmap
            )
            _show_recovery_warnings(converted_doc.warnings)

            # Output
            generator = OutputGenerator(include_metadata=True)
            content = generator.generate(
                converted_doc,
                encoding_result,
                output_format=OutputFormat.TEXT,
            )
            generator.save(content, output)

        print_success(f"Unicode output saved to: {output}")

    except Exception as e:
        print_error(str(e))
        sys.exit(1)


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path. Defaults to input file with new extension.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "markdown", "md", "pdf"]),
    default="text",
    help="Output format (text, markdown, or pdf).",
)
@click.option(
    "--encoding",
    type=str,
    default=None,
    help="Force specific encoding (e.g., shree-lipi, kruti-dev).",
)
@click.option(
    "--use-ocr",
    is_flag=True,
    help="Use OCR to extract text from PDF (useful for scanned documents).",
)
@click.option(
    "--ocr-lang",
    type=str,
    default="mar",
    help="OCR language code (e.g., 'mar' for Marathi, 'hin' for Hindi). Default: mar",
)
@click.option(
    "--ocr-dpi",
    type=int,
    default=300,
    help="DPI for OCR rendering (higher = better quality but slower). Default: 300",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress progress output.",
)
@click.option(
    "--strict-cmap", is_flag=True, help="Refuse recovery that uses inferred cmap mappings."
)
def extract(
    strict_cmap: bool,
    input_file: Path,
    output: Path | None,
    output_format: str,
    encoding: str | None,
    use_ocr: bool,
    ocr_lang: str,
    ocr_dpi: int,
    quiet: bool,
):
    """Extract text from PDF (OCR or font-based) without translation.

    This command extracts text from PDFs using either:
    - OCR mode (--use-ocr): Uses Tesseract to recognize text in scanned documents
    - Font-based mode (default): Extracts embedded text and converts legacy encodings to Unicode

    Examples:
        # Extract with OCR (Marathi)
        legacylipi extract input.pdf --use-ocr -o marathi.txt

        # Extract with OCR (Hindi) at higher quality
        legacylipi extract input.pdf --use-ocr --ocr-lang hin --ocr-dpi 600 -o output.txt

        # Extract with font-based extraction (auto-detect encoding)
        legacylipi extract input.pdf -o output.txt

        # Extract to PDF format (preserves structure)
        legacylipi extract input.pdf --use-ocr -o output.pdf --format pdf
    """
    if not quiet:
        print_banner()

    # Validate OCR requirements if using OCR mode
    if use_ocr:
        available, msg = check_tesseract_available()
        if not available:
            print_error(msg)
            console.print("\n[dim]To install Tesseract on Ubuntu/Debian:[/dim]")
            console.print("  sudo apt-get install tesseract-ocr tesseract-ocr-mar")
            console.print("\n[dim]On macOS:[/dim]")
            console.print("  brew install tesseract tesseract-lang")
            sys.exit(1)

        if not check_language_available(ocr_lang):
            print_error(f"OCR language '{ocr_lang}' is not available.")
            available_langs = get_available_languages()
            if available_langs:
                console.print(
                    f"\n[dim]Available languages:[/dim] {', '.join(available_langs[:10])}"
                )
            console.print("\n[dim]To install Marathi language pack:[/dim]")
            console.print("  sudo apt-get install tesseract-ocr-mar")
            sys.exit(1)

    # Determine output format
    if output_format == "pdf":
        fmt = OutputFormat.PDF
        default_ext = ".pdf"
    elif output_format in ("markdown", "md"):
        fmt = OutputFormat.MARKDOWN
        default_ext = ".md"
    else:
        fmt = OutputFormat.TEXT
        default_ext = ".txt"

    # Determine output path
    if output is None:
        suffix = ".extracted" + default_ext
        output = input_file.with_suffix(suffix)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=quiet,
        ) as progress:
            # Extract text (with OCR or standard text extraction)
            if use_ocr:
                task = progress.add_task(f"Running OCR ({ocr_lang})...", total=None)
                document = parse_pdf_with_ocr(input_file, lang=ocr_lang, dpi=ocr_dpi)
                progress.update(
                    task, description=f"[green]✓[/green] OCR extracted {document.page_count} pages"
                )

                if not quiet:
                    console.print(
                        f"\n[dim]📄 Input:[/dim] {input_file.name} ({document.page_count} pages)"
                    )
                    console.print(f"[dim]👁 OCR:[/dim] {ocr_lang} @ {ocr_dpi} DPI")

                # In OCR mode, text is already Unicode
                from legacylipi.core.models import DetectionMethod, EncodingDetectionResult

                encoding_result = EncodingDetectionResult(
                    detected_encoding="unicode-ocr",
                    confidence=1.0,
                    method=DetectionMethod.UNICODE_DETECTED,
                )
                converted_doc = document
            else:
                task = progress.add_task("Parsing PDF...", total=None)
                document = parse_pdf(input_file)
                progress.update(
                    task, description=f"[green]✓[/green] Parsed {document.page_count} pages"
                )

                if not quiet:
                    console.print(
                        f"\n[dim]📄 Input:[/dim] {input_file.name} ({document.page_count} pages)"
                    )

                # Detect encoding
                progress.update(task, description="Detecting encoding...")
                detector = EncodingDetector()

                if encoding:
                    from legacylipi.core.models import DetectionMethod, EncodingDetectionResult

                    encoding_result = EncodingDetectionResult(
                        detected_encoding=encoding,
                        confidence=1.0,
                        method=DetectionMethod.USER_OVERRIDE,
                    )
                    page_encodings = {page.page_number: encoding_result for page in document.pages}
                else:
                    encoding_result, page_encodings = detector.detect_from_document(document)

                progress.update(
                    task,
                    description=f"[green]✓[/green] Encoding: {encoding_result.detected_encoding} ({encoding_result.confidence:.0%})",
                )

                if not quiet:
                    console.print(
                        f"[dim]🎯 Encoding:[/dim] {encoding_result.detected_encoding} (confidence: {encoding_result.confidence:.0%})"
                    )

                # Convert to Unicode
                progress.update(task, description="Converting to Unicode...")
                converter = UnicodeConverter()
                converted_doc = converter.convert_with_recovery(
                    document,
                    encoding_result,
                    page_encodings=page_encodings,
                    strict_cmap=strict_cmap,
                )
                _show_recovery_warnings(converted_doc.warnings)
                progress.update(task, description="[green]✓[/green] Converted to Unicode")

            # Generate output (no translation)
            progress.update(task, description="Generating output...")
            generator = OutputGenerator()
            output_content = generator.generate(
                converted_doc,
                encoding_result,
                translation_result=None,  # No translation
                output_format=fmt,
            )

            _show_recovery_warnings(generator.warnings)

            # Save output
            generator.save(output_content, output)
            progress.update(task, description=f"[green]✓[/green] Saved to {output.name}")

        print_success(f"Output saved to: {output}")

        # Print summary if not quiet
        if not quiet:
            console.print("\n[bold]📊 Summary:[/bold]")
            console.print(f"   • Pages processed: {document.page_count}")
            console.print(f"   • Extraction method: {'OCR' if use_ocr else 'Font-based'}")
            if not use_ocr:
                console.print(f"   • Encoding: {encoding_result.detected_encoding}")
            console.print(f"   • Output format: {output_format}")

    except PDFParseError as e:
        print_error(f"Failed to parse PDF: {e}")
        sys.exit(1)
    except OCRError as e:
        print_error(f"OCR failed: {e}")
        console.print("\n[dim]Suggestions:[/dim]")
        console.print("  • Ensure Tesseract is installed: sudo apt-get install tesseract-ocr")
        console.print("  • Install language pack: sudo apt-get install tesseract-ocr-mar")
        console.print("  • Try without --use-ocr to use font-based extraction")
        sys.exit(1)
    except Exception as e:
        print_error(str(e))
        if not quiet and not isinstance(e, ValueError):
            console.print_exception()
        sys.exit(1)


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed page-by-page analysis.",
)
def detect(input_file: Path, verbose: bool):
    """Detect the font encoding used in a PDF.

    Analyzes the PDF to identify whether it uses legacy font encoding
    and which specific encoding family it belongs to.
    """
    print_banner()

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Analyzing...", total=None)

            # Parse PDF
            document = parse_pdf(input_file)
            progress.update(task, description=f"Parsed {document.page_count} pages")

            # Detect encoding
            detector = EncodingDetector()
            encoding_result, page_encodings = detector.detect_from_document(document)

        # Display results
        console.print(f"\n[bold]📄 File:[/bold] {input_file.name}")
        console.print(f"[bold]📊 Pages:[/bold] {document.page_count}")

        # Overall result
        console.print("\n[bold]🎯 Detected Encoding:[/bold]")
        console.print(f"   Encoding: [cyan]{encoding_result.detected_encoding}[/cyan]")
        console.print(
            f"   Confidence: [{'green' if encoding_result.confidence > 0.8 else 'yellow'}]{encoding_result.confidence:.1%}[/]"
        )
        console.print(f"   Method: {encoding_result.method.value}")

        if encoding_result.detected_encoding == "glyph-id":
            console.print(
                "\n[yellow]Embedded font has glyph IDs but no ToUnicode mapping.[/yellow]"
            )
            console.print(f"   Use: legacylipi recover-text {input_file.name} -o recovered.txt")
        elif encoding_result.is_unicode:
            console.print("\n[green]✓ Document is already in Unicode format.[/green]")
        elif encoding_result.is_legacy:
            console.print("\n[yellow]⚠ Legacy encoding detected.[/yellow]")
            console.print(f"   Use: [dim]legacylipi translate {input_file.name}[/dim]")

        # Page-by-page analysis if verbose
        if verbose and page_encodings:
            console.print("\n[bold]Page-by-Page Analysis:[/bold]")

            table = Table(show_header=True, header_style="bold")
            table.add_column("Page", justify="right")
            table.add_column("Encoding")
            table.add_column("Confidence", justify="right")
            table.add_column("Method")

            for page_num, result in sorted(page_encodings.items()):
                conf_style = "green" if result.confidence > 0.8 else "yellow"
                table.add_row(
                    str(page_num),
                    result.detected_encoding,
                    f"[{conf_style}]{result.confidence:.0%}[/]",
                    result.method.value,
                )

            console.print(table)

        # Fonts found
        if document.fonts:
            console.print("\n[bold]Fonts Found:[/bold]")
            for font in document.fonts[:10]:  # Limit to first 10
                console.print(f"   • {font.name}")
            if len(document.fonts) > 10:
                console.print(f"   [dim]... and {len(document.fonts) - 10} more[/dim]")

    except Exception as e:
        print_error(str(e))
        sys.exit(1)


def _show_recovery_warnings(warnings: list[dict]) -> None:
    for warning in warnings:
        console.print(
            f"[yellow]Warning: {warning['message']} {', '.join(warning.get('codepoints', []))}[/yellow]"
        )


def _atomic_text_outputs(outputs: dict[Path, str]) -> None:
    """Stage every output, then replace; roll back earlier replacements on error.

    Each rename is atomic. Filesystems cannot atomically rename two paths as a
    transaction; backups protect against ordinary synchronous write failures.
    """
    import os
    import tempfile

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, text in outputs.items():
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as handle:
                staged[path] = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists() or path.is_symlink():
                # A hard link preserves the original inode, bytes and metadata,
                # including a symlink itself rather than its target.
                fd, name = tempfile.mkstemp(dir=path.parent)
                os.close(fd)
                backup = Path(name)
                backup.unlink()
                os.link(path, backup, follow_symlinks=False)
                backups[path] = backup
        try:
            for path, temporary in staged.items():
                os.replace(temporary, path)
                replaced.append(path)
        except OSError as exc:
            failures = []
            for path in reversed(replaced):
                try:
                    if path in backups:
                        os.replace(backups[path], path)
                    else:
                        path.unlink()
                except OSError as rollback_error:
                    failures.append(f"{path}: {rollback_error}; original at {backups.get(path)}")
                    backups.pop(path, None)  # Keep the backup for manual restoration.
            if failures:
                raise OSError(
                    f"Output replacement failed: {exc}; rollback failed: {'; '.join(failures)}"
                ) from exc
            raise OSError(f"Output replacement failed; original outputs restored: {exc}") from exc
    finally:
        for temporary in (*staged.values(), *backups.values()):
            temporary.unlink(missing_ok=True)


@main.command("recover-text")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--strict-cmap", is_flag=True, help="Refuse recovery that uses inferred cmap mappings."
)
def recover_text(input_file: Path, output: Path, report_path: Path | None, strict_cmap: bool):
    """Recover Telugu Unicode from embedded glyph-ID fonts without ToUnicode.

    Writes a JSON report beside OUTPUT unless --report selects another path.
    """
    import json

    from legacylipi.core.glyph_recovery import recover_pdf

    report_path = report_path or output.with_suffix(".report.json")
    paths = [input_file, output, report_path]
    try:
        for i, path in enumerate(paths):
            for other in paths[i + 1 :]:
                if path.resolve() == other.resolve() or (
                    path.exists() and other.exists() and path.samefile(other)
                ):
                    raise ValueError(
                        "Input, output and report paths must be different (including inode aliases)."
                    )
        for path in (output, report_path):
            if not path.parent.is_dir():
                raise ValueError(f"Destination directory does not exist: {path.parent}")
            if path.exists() and not path.is_file():
                raise ValueError(f"Destination is not a regular file: {path}")
            if path.exists():
                with path.open("rb") as handle:
                    if b"%PDF-" in handle.read(1024):
                        raise ValueError(
                            "Refusing to overwrite an existing PDF, regardless of extension."
                        )
        recovered = recover_pdf(input_file, strict_cmap=strict_cmap)
        _show_recovery_warnings(recovered.warnings)
        _atomic_text_outputs(
            {
                output: recovered.text,
                report_path: json.dumps(recovered.report, ensure_ascii=False, indent=2) + "\n",
            }
        )
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    report = recovered.report
    table = Table(title="Glyph-ID recovery")
    table.add_column("Metric")
    table.add_column("Result", justify="right")
    for label, value in (
        ("Fonts found", str(len(report["fonts"]))),
        ("Glyph coverage", f"{report['glyph_coverage']:.2%}"),
        (
            "Round-trip match",
            f"{report['roundtrip_match_ratio']:.2%}"
            if report["verification_complete"]
            else "incomplete",
        ),
        ("Unknown glyphs", str(report["unknown_count"])),
        ("Collisions", str(report["collisions"])),
        ("Inferred cmap entries", str(report["inferred_cmap_entries"])),
        ("Well-formedness", f"{report['well_formedness_ratio']:.2%}"),
    ):
        table.add_row(label, value)
    console.print(table)
    console.print(report["verification_note"], markup=False)
    console.print(f"Text: {output}\nReport: {report_path}", markup=False)


@main.command("scan-copy")
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path. Defaults to input file with '_scanned.pdf' suffix.",
)
@click.option(
    "--dpi",
    type=click.Choice(["150", "300", "600"]),
    default="300",
    help="DPI for rendering (higher = better quality but larger file). Default: 300",
)
@click.option(
    "--color-mode",
    type=click.Choice(["color", "grayscale", "bw"]),
    default="color",
    help="Color mode: color, grayscale, or bw (black & white). Default: color",
)
@click.option(
    "--quality",
    type=click.IntRange(1, 100),
    default=85,
    help="JPEG quality (1-100). Lower = smaller file. Default: 85",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress progress output.",
)
def scan_copy(
    input_file: Path,
    output: Path | None,
    dpi: str,
    color_mode: str,
    quality: int,
    quiet: bool,
):
    """Create a scanned copy of a PDF (image-based, no text).

    Renders each page as an image and creates a new image-based PDF.
    Useful for preserving visual appearance or creating archival copies.
    """
    if not quiet:
        print_banner()
        console.print(f"\n[bold]Creating scanned copy of:[/bold] {input_file.name}")

    # Determine output path
    if output is None:
        output = input_file.with_stem(f"{input_file.stem}_scanned")

    try:
        generator = OutputGenerator()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=quiet,
        ) as progress:
            task = progress.add_task("Rendering pages...", total=None)

            generator.generate_scanned_copy(
                input_path=input_file,
                output_path=output,
                dpi=int(dpi),
                color_mode=color_mode,
                quality=quality,
            )

            progress.update(task, description="Complete!")

        if not quiet:
            print_success(f"Scanned copy saved to: {output}")

            # Show file size comparison
            original_size = input_file.stat().st_size / 1024
            output_size = output.stat().st_size / 1024
            console.print(f"  Original: {original_size:.1f} KB")
            console.print(f"  Output:   {output_size:.1f} KB")

    except Exception as e:
        print_error(str(e))
        sys.exit(1)


@main.command("encodings")
@click.option(
    "--search",
    "-s",
    type=str,
    default=None,
    help="Search for encoding by name.",
)
def list_encodings(search: str | None):
    """List supported font encodings.

    Shows all font encoding families that LegacyLipi can detect and convert.
    """
    print_banner()

    loader = MappingLoader()
    encodings = loader.list_available()

    if search:
        encodings = [e for e in encodings if search.lower() in e.lower()]

    console.print(f"\n[bold]Supported Encodings ({len(encodings)}):[/bold]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Encoding Name")
    table.add_column("Font Family")
    table.add_column("Language")
    table.add_column("Type")

    from legacylipi.mappings.loader import BUILTIN_MAPPINGS

    for encoding in encodings:
        if encoding in BUILTIN_MAPPINGS:
            mapping = BUILTIN_MAPPINGS[encoding]
            table.add_row(
                encoding,
                mapping.font_family,
                mapping.language,
                "[green]Built-in[/green]",
            )
        else:
            table.add_row(
                encoding,
                "-",
                "-",
                "[dim]External[/dim]",
            )

    console.print(table)

    console.print("\n[dim]Use --encoding <name> with translate/convert commands[/dim]")


@main.command()
@click.option("--port", default=8000, type=int, help="Port to run the API on.")
@click.option("--host", default="0.0.0.0", help="Host to bind to.")
def api(port: int, host: str):
    """Launch the LegacyLipi REST API + React frontend."""
    from legacylipi.api.main import serve

    print_banner()
    console.print(f"[bold green]Starting API server on http://{host}:{port}[/bold green]")
    serve(host=host, port=port)


@main.command()
@click.option(
    "--service",
    type=str,
    default="gcp_translate",
    help="Service to check usage for (default: gcp_translate).",
)
def usage(service: str):
    """Show current API usage statistics.

    Displays monthly character usage for translation services.
    """
    print_banner()

    from legacylipi.core.utils.usage_tracker import UsageTracker

    tracker = UsageTracker()
    summary = tracker.get_usage_summary(service)

    console.print(f"\n[bold]📊 Usage Statistics for {service}:[/bold]\n")
    console.print(f"   Month: {summary['month']}")
    console.print(f"   Characters used: {summary['characters']:,}")

    if service == "gcp_translate":
        limit = 500_000
        remaining = limit - summary["characters"]
        pct = (summary["characters"] / limit) * 100

        color = "green" if pct < 80 else "yellow" if pct < 100 else "red"
        console.print(f"   Free tier limit: {limit:,}")
        console.print(f"   Remaining: [{color}]{remaining:,}[/]")
        console.print(f"   Usage: [{color}]{pct:.1f}%[/]")

    if summary.get("last_updated"):
        console.print(f"   Last updated: {summary['last_updated']}")


if __name__ == "__main__":
    main()
