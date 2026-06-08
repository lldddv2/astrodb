# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with uv)
uv pip install -e .

# Run TUI
astrodb
# or directly
python src/astrodb/main.py

# Run tests
uv run pytest
# Single test
uv run pytest tests/path/to/test.py::test_name
```

## Architecture

Clean Architecture with three layers:

- `src/astrodb/ui/` — Textual components. `app.py` defines the three panels (`HeaderPanel`, `EditorPanel`, `SidebarPanel`) assembled in `AstroDbApp`. Styles live in `app.tcss`.
- `src/astrodb/domain/` — Business logic, models, interfaces. No framework dependencies.
- `src/astrodb/infrastructure/` — Concrete implementations: `clients/` for pyVO ADQL connections, `storage/` for SQLite/CSV persistence.

Entry point: `main.py:cli()` → `ui/app.py:main()` → `AstroDbApp.run()`.

## Key Constraints

- Target DBs: VizieR, Simbad, and one TBD astronomical VO service — all accessed via ADQL over pyVO.
- Data flow: Query → Preview → Clean (drop nulls) → Export (`.db` or `.csv`).
- UI layout is fixed by mockup: header (DB selector) / left panel (ADQL editor, 3fr) / right sidebar (table + columns, 1fr).
- `textual` widgets must stay in `ui/`; domain logic must not import from `ui/` or `infrastructure/`.
- Python 3.10+ required; build backend is `hatchling`.
