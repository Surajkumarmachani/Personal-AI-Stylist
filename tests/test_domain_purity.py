"""packages/stylist_domain must not import db, api, or clients.

That boundary is what makes the scorer and slot rules unit-testable without a
database, and it keeps the deterministic path independent of the AI path. It is
easy to violate accidentally with one convenience import, so it is a test.
"""

from __future__ import annotations

import ast
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[1] / "packages" / "stylist_domain"
FORBIDDEN_PREFIXES = (
    "stylist_db",
    "stylist_api",
    "stylist_clients",
    "sqlalchemy",
    "fastapi",
    "redis",
    "boto3",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_domain_has_no_infrastructure_imports() -> None:
    violations: list[str] = []
    for py in sorted(DOMAIN.rglob("*.py")):
        for module in _imported_modules(py):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{py.relative_to(DOMAIN.parent.parent)} imports {module}")
    assert not violations, "stylist_domain must stay pure:\n" + "\n".join(violations)


def test_domain_is_importable_without_a_database() -> None:
    """The real assertion behind the rule: loading the taxonomy needs nothing
    but a YAML file on disk."""
    from stylist_domain.taxonomy import load_taxonomy

    tax = load_taxonomy()
    assert tax.version
    assert len(tax.slots) == 9
    assert "saree" in tax.subcategories
    assert tax.default_slot_for("saree") == "drape"
