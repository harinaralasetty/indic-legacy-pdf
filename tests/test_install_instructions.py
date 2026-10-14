"""Install instructions must not resolve to the unrelated PyPI package.

The PyPI project named ``legacylipi`` is a different project (see README), so
any command that installs by bare name would fetch someone else's code. Docs
and the release workflow must install from this repository's Git URL, and the
release workflow must not publish to PyPI under that name.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"

INSTALL_COMMAND = re.compile(r"\b(pip install|uv tool install|uvx)\b")


def _bare_name_installs(path: Path) -> list[str]:
    """Lines that install ``legacylipi`` without pointing at a Git URL."""
    offenders = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if (
            INSTALL_COMMAND.search(line)
            and "legacylipi" in line
            and "git+https://" not in line
        ):
            offenders.append(f"{path.name}:{number}: {line.strip()}")
    return offenders


def test_release_workflow_does_not_publish_to_pypi() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "pypa/gh-action-pypi-publish" not in text
    assert "PYPI_API_TOKEN" not in text


def test_release_notes_install_from_git_not_pypi() -> None:
    assert _bare_name_installs(RELEASE_WORKFLOW) == []


def test_readme_install_commands_install_from_git_not_pypi() -> None:
    assert _bare_name_installs(README) == []
