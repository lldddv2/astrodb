# Agent Instructions (`AGENTS.md`)

## Project Context
- **Domain:** Astronomy Data Mining (`mineria_de_datos/proyecto_final/astrodb`).
- **Purpose:** TUI (Terminal User Interface) tool for centralizing queries, cleaning, and persisting astronomical database records.
- **Core Features:**
  - Database selection (target: 3 specific astronomical DBs).
  - ADQL query execution with syntax highlighting.
  - Column name discovery (sidebar).
  - Data cleaning (e.g., null removal).
  - Export formats: SQLite (`.db`) and CSV.
  - Preview capabilities before saving.

## Setup & Commands
- **Environment:** Use `uv` (recommended) or `pip` with `pyproject.toml`.
- **Install:** `pip install -e .` (or `uv pip install -e .`)
- **Run:** `astrodb` (after installing) or `python src/astrodb/main.py`

## Architecture & Conventions
- **Language & Framework:** Python 3.10+ using `textual` for the TUI and `pyvo` for ADQL/astronomy queries.
- **Pattern:** Clean Architecture (UI decoupled from business logic).
  - `src/astrodb/ui/`: Textual components (App, Editor, Sidebar).
  - `src/astrodb/domain/`: Core business logic (models, interfaces).
  - `src/astrodb/infrastructure/`: Concrete implementations (PyVO clients, SQLite/CSV storage).
- **UI Pattern:** TUI with a specific mockup layout:
  - **Header:** Database selector (e.g., `BASE DE DATOS: vizier`).
  - **Main Panel (Left):** ADQL query editor with syntax highlighting.
  - **Sidebar (Right):** Table indicator (`TABLA: ...`) and a list of available columns (`Columnas: ...`) for the selected table.
- **Data Flow:** Query DB -> Preview -> Clean (remove nulls) -> Persist (.db/.csv).

## Agent Guidelines
- Prefer editing existing files over creating new ones unless instructed.
- Focus on modularizing the DB connection, ADQL parsing, and UI components separately.
