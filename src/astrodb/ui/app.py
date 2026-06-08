from __future__ import annotations
import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from rich.text import Text

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Rule,
    Static,
    TextArea,
)
from textual.worker import Worker
from typing import Iterable

_LAVENDER = "#D4BBFB"
_MUTED_ORANGE = "#D08A55"

_LOGO_ASTRO = [
    "╔═╗╔═╗╔╦╗╦═╗╔═╗",
    "╠═╣╚═╗ ║ ╠╦╝║ ║",
    "╩ ╩╚═╝ ╩ ╩╚═╚═╝",
]
_LOGO_DB = [
    "╔╦╗╔╗ ",
    " ║║╠╩╗",
    "═╩╝╚═╝",
]

_DB_COLORS: dict[str, str] = {
    "sdss": "#E89D3A",
    "vizier": "#5DADE2",
    "nasa": "#FC3D21",
}

_EXT_COLORS: dict[str, str] = {
    "csv": "#5DADE2",
    "db": "#E89D3A",
}

from astrodb.domain.history import QueryHistoryEntry
from astrodb.domain.models import DatabaseConfig, QueryResult
from astrodb.infrastructure.clients.pyvo_client import (
    HTTPQueryClient,
    QueryCancelled,
    TransientServerError,
)
from astrodb.infrastructure.storage.adql_storage import AdqlStorage
from astrodb.infrastructure.storage.csv_storage import CSVStorage
from astrodb.infrastructure.storage.sqlite_storage import SQLiteStorage

DATABASES: list[DatabaseConfig] = [
    DatabaseConfig(
        name="sdss",
        display_name="SDSS DR18",
        query_url=(
            "http://skyserver.sdss.org/dr18/SkyServerWS/SearchTools/"
            "SqlSearch?format=csv&cmd="
        ),
        columns_mode="select_top1",
    ),
    DatabaseConfig(
        name="vizier",
        display_name="VizieR TAP",
        query_url=(
            "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
            "?request=doQuery&lang=ADQL&format=csv&query="
        ),
        columns_mode="tap_schema",
    ),
    DatabaseConfig(
        name="nasa",
        display_name="NASA Exoplanet",
        query_url="https://exoplanetarchive.ipac.caltech.edu/TAP/sync?format=csv&query=",
        columns_mode="tap_schema",
    ),
]

# Catches: "table", 'table', table, schema.table
_FROM_RE = re.compile(
    r'\bFROM\s+(?:"([^"]+)"|\'([^\']+)\'|([^\s,;()]+))',
    re.IGNORECASE,
)


_SELECT_RE = re.compile(
    r"\bSELECT\b\s+(?:TOP\s+\d+\s+)?(.*?)\bFROM\b",
    re.IGNORECASE | re.DOTALL,
)

_ALIAS_RE = re.compile(r"\s+AS\s+\w+$", re.IGNORECASE)

_TOP_RE = re.compile(r"\bSELECT\b(\s+DISTINCT)?\s+(?:TOP\s+\d+\s+)?", re.IGNORECASE)


def _inject_top(adql: str, n: int) -> str:
    def repl(match: re.Match) -> str:
        distinct_group = match.group(1)
        return f"SELECT{distinct_group or ''} TOP {n} "
    return _TOP_RE.sub(repl, adql, count=1)


def _validate_local(adql: str) -> str | None:
    if not adql.strip():
        return "consulta vacía"
    if adql.rstrip().endswith(";"):
        return "quitá el ';' final, rompe el TAP sync"
    depth = 0
    in_single = False
    in_double = False
    for ch in adql:
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == '"':
                in_double = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    if depth != 0:
        return "paréntesis sin balancear"
    if in_single or in_double:
        return "comilla sin cerrar"
    if not _SELECT_RE.search(adql):
        return "falta SELECT … FROM"
    if _parse_table(adql) is None:
        return "no se detectó tabla en FROM"
    try:
        import sqlglot
        try:
            list(sqlglot.tokens.Tokenizer().tokenize(adql))
        except sqlglot.errors.TokenError as exc:
            return f"token inválido: {exc}"
    except ImportError:
        pass
    return None

_ADQL_EXTRA_HIGHLIGHTS = """
((identifier) @keyword
 (#any-of? @keyword "TOP" "POINT" "CIRCLE" "BOX" "POLYGON" "REGION" "CONTAINS" "INTERSECTS" "DISTANCE" "AREA" "COORD1" "COORD2" "COORDSYS" "CENTROID"))
"""


def _parse_table(adql: str) -> str | None:
    m = _FROM_RE.search(adql)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    return None


def _parse_select_columns(adql: str) -> list[str] | None:
    """
    Extract bare column names from SELECT clause.
    Returns None if no SELECT…FROM, ["*"] for wildcard, or list of names.
    MAX(col) → "col"; COUNT(*) skipped.
    """
    m = _SELECT_RE.search(adql)
    if not m:
        return None
    body = m.group(1).strip()
    if body == "*":
        return ["*"]

    parts: list[str] = []
    depth = 0
    buf = ""
    for ch in body + ",":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch

    out: list[str] = []
    for p in parts:
        if not p:
            continue
        p = _ALIAS_RE.sub("", p).strip()
        p = p.split()[0] if p.split() else p
        fn_match = re.match(r"^\w+\(([^)]*)\)$", p)
        if fn_match:
            inner = fn_match.group(1).strip()
            if inner == "*" or not inner:
                continue
            p = inner.split(",")[0].strip()
        p = p.split(".")[-1]
        p = p.strip('"').strip("`").strip("'")
        if p:
            out.append(p)
    return out if out else ["*"]


# ---------------------------------------------------------------------------
# ADQL-aware TextArea subclass
# ---------------------------------------------------------------------------

def _sys_clip_copy(text: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        elif sys.platform.startswith("linux"):
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
                try:
                    subprocess.run(cmd, input=text.encode(), check=True)
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
        elif sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=False)
    except Exception:
        pass


def _sys_clip_paste() -> str:
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, check=False)
            return r.stdout.decode(errors="replace")
        if sys.platform.startswith("linux"):
            for cmd in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, check=True)
                    return r.stdout.decode(errors="replace")
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
        if sys.platform == "win32":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True, check=False,
            )
            return r.stdout.decode(errors="replace").rstrip("\r\n")
    except Exception:
        pass
    return ""


class ADQLTextArea(TextArea):
    """TextArea: ADQL highlight, Tab focus cycle, Cmd line nav."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("tab", "shift+tab"):
            event.prevent_default()
            event.stop()
            self.app.action_cycle_focus(event.key == "shift+tab")
            return
        if event.key in ("super+right", "cmd+right"):
            event.prevent_default()
            event.stop()
            self.action_cursor_line_end()
            return
        if event.key in ("super+left", "cmd+left"):
            event.prevent_default()
            event.stop()
            self.action_cursor_line_start()
            return
        await super()._on_key(event)

    def action_copy(self) -> None:
        text = self.selected_text
        if not text:
            return
        _sys_clip_copy(text)
        try:
            self.app.copy_to_clipboard(text)
        except Exception:
            pass

    def action_cut(self) -> None:
        if self.read_only:
            return
        text = self.selected_text
        if text:
            _sys_clip_copy(text)
        super().action_cut()

    def action_paste(self) -> None:
        if self.read_only:
            return
        clipboard = _sys_clip_paste() or self.app.clipboard
        if not clipboard:
            return
        result = self._replace_via_keyboard(clipboard, *self.selection)
        if result is not None:
            self.move_cursor(result.end_location)

    def on_mount(self) -> None:
        try:
            from textual.widgets._text_area import TREE_SITTER, get_language, _HIGHLIGHTS_PATH
            if TREE_SITTER:
                sql_lang = get_language("sql")
                if sql_lang is not None:
                    base_q = (_HIGHLIGHTS_PATH / "sql.scm").read_text()
                    self.register_language("adql", sql_lang, base_q + _ADQL_EXTRA_HIGHLIGHTS)
                    # call_after_refresh ensures this fires after the initial "sql" reactive watcher
                    self.call_after_refresh(self._activate_adql)
        except Exception:
            pass

    def _activate_adql(self) -> None:
        self.language = "adql"


# ---------------------------------------------------------------------------
# Export modal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UI panels
# ---------------------------------------------------------------------------

class NavDirectoryTree(DirectoryTree):
    BINDINGS = [
        Binding("backspace", "go_up", "Subir", show=False),
        Binding("minus", "go_up", "Subir", show=False),
    ]

    def go_up(self) -> None:
        current = Path(self.path)
        parent = current.parent
        if parent != current:
            self.path = str(parent)
            self.reload()
            self.post_message(DirectoryTree.DirectorySelected(self.root, parent))

    def action_go_up(self) -> None:
        self.go_up()


class AdqlDirectoryTree(NavDirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir() or p.suffix == ".adql"]


class LoadAdqlModal(ModalScreen[Path | None]):
    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    def compose(self) -> ComposeResult:
        with Vertical(id="load-modal"):
            yield Label("[bold]▾ Cargar consulta (.adql)[/bold]", id="load-modal-title")
            yield AdqlDirectoryTree(str(Path.cwd()), id="adql-tree")
            with Horizontal(id="load-modal-buttons"):
                yield Button("✗ Cancelar", id="btn-load-cancel", variant="error")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if event.path.suffix == ".adql":
            self.dismiss(event.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-load-cancel":
            self.dismiss(None)


class SaveQueryModal(ModalScreen[Path | None]):
    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    _selected_dir: Path

    def __init__(self, default_name: str = "query") -> None:
        super().__init__()
        self._selected_dir = Path.cwd()
        self._default_name = default_name

    def compose(self) -> ComposeResult:
        with Vertical(id="load-modal"):
            yield Label("[bold]▾ Guardar consulta (.adql)[/bold]", id="load-modal-title")
            with Horizontal(id="save-name-row"):
                yield Label("[bold]Nombre:[/bold]", id="save-name-label")
                yield Input(value=self._default_name, id="save-filename-input")
            with Horizontal(id="save-dir-bar"):
                yield Button("↑ ..", id="btn-dir-up", variant="default")
                yield Label(f"[dim]→ {self._selected_dir}[/dim]", id="save-dir-current")
            yield NavDirectoryTree(str(Path.cwd()), id="save-dir-tree")
            with Horizontal(id="load-modal-buttons"):
                yield Button("✓ Guardar", id="btn-save-here", variant="primary")
                yield Button("✗ Cancelar", id="btn-save-cancel", variant="error")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._selected_dir = event.path
        self.query_one("#save-dir-current", Label).update(f"[dim]→ {event.path}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save-here":
            name = self.query_one("#save-filename-input", Input).value.strip() or "query"
            self.dismiss(self._selected_dir / name)
        elif event.button.id == "btn-save-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-dir-up":
            self.query_one("#save-dir-tree", NavDirectoryTree).go_up()


class ExportResultsModal(ModalScreen[tuple[Path, Literal["csv", "db"], str] | None]):
    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    _selected_dir: Path
    _selected_ext: Literal["csv", "db"] = "csv"

    def __init__(
        self,
        default_name: str | None = None,
        default_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._selected_dir = default_dir if default_dir is not None else Path.cwd()
        self._default_name = default_name if default_name else "results"

    def compose(self) -> ComposeResult:
        with Vertical(id="load-modal"):
            yield Label("[bold]▾ Exportar resultados[/bold]", id="load-modal-title")
            with Horizontal(id="export-name-row"):
                yield Label("[bold]Nombre del archivo:[/bold]")
                yield Input(value=self._default_name, id="export-filename-input")
            with Horizontal(id="export-type-row"):
                yield Label("[bold]tipo:[/bold]")
                yield Button(".csv", id="exp-csv", variant="primary")
                yield Button(".db", id="exp-db", variant="default")
            with Horizontal(id="export-table-row", classes="hidden"):
                yield Label("[bold]Nombre de la tabla:[/bold]")
                yield Input(value="results", id="export-table-input")
            with Horizontal(id="save-dir-bar"):
                yield Button("↑ ..", id="btn-dir-up", variant="default")
                yield Label(f"[dim]→ {self._selected_dir}[/dim]", id="save-dir-current")
            yield NavDirectoryTree(str(self._selected_dir), id="save-dir-tree")
            with Horizontal(id="load-modal-buttons"):
                yield Button("✓ Exportar", id="btn-export-go", variant="primary")
                yield Button("✗ Cancelar", id="btn-export-cancel", variant="error")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._selected_dir = event.path
        self.query_one("#save-dir-current", Label).update(f"[dim]→ {event.path}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "exp-csv":
            self._selected_ext = "csv"
            self.query_one("#exp-csv", Button).variant = "primary"
            self.query_one("#exp-db", Button).variant = "default"
            self.query_one("#export-table-row").add_class("hidden")
        elif bid == "exp-db":
            self._selected_ext = "db"
            self.query_one("#exp-csv", Button).variant = "default"
            self.query_one("#exp-db", Button).variant = "primary"
            self.query_one("#export-table-row").remove_class("hidden")
        elif bid == "btn-export-go":
            name = self.query_one("#export-filename-input", Input).value.strip() or "results"
            table = self.query_one("#export-table-input", Input).value.strip() or "results"
            self.dismiss((self._selected_dir / name, self._selected_ext, table))
        elif bid == "btn-export-cancel":
            self.dismiss(None)
        elif bid == "btn-dir-up":
            self.query_one("#save-dir-tree", NavDirectoryTree).go_up()


class CancelPickerModal(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    def __init__(self, jobs: list[tuple[str, str, float]]) -> None:
        super().__init__()
        self._jobs = jobs

    def compose(self) -> ComposeResult:
        with Vertical(id="load-modal"):
            yield Label("[bold]■ Cancelar consulta[/bold]", id="load-modal-title")
            yield Label("[dim]Elegí cuál cancelar:[/dim]")
            lv = ListView(id="cancel-picker-list")
            yield lv
            with Horizontal(id="load-modal-buttons"):
                yield Button("✗ Volver", id="btn-cancel-picker-back", variant="error")

    def on_mount(self) -> None:
        lv = self.query_one("#cancel-picker-list", ListView)
        for jid, snippet, elapsed in self._jobs:
            safe_id = f"cp-{jid}"
            lv.append(
                ListItem(
                    Static(f"[yellow]●[/] {snippet}  ⏱ {elapsed:.1f}s", markup=True),
                    id=safe_id,
                )
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("cp-"):
            self.dismiss(item_id[3:])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel-picker-back":
            self.dismiss(None)


class LogoWidget(Static):
    def on_mount(self) -> None:
        text = Text()
        for i in range(len(_LOGO_ASTRO)):
            text.append(_LOGO_ASTRO[i], style=f"bold {_LAVENDER}")
            text.append(_LOGO_DB[i], style=f"bold {_MUTED_ORANGE}")
            if i < len(_LOGO_ASTRO) - 1:
                text.append("\n")
        self.update(text)


class HeaderPanel(Static):
    def compose(self) -> ComposeResult:
        with Horizontal(id="header-inner"):
            yield LogoWidget(id="logo")
            with Vertical(id="header-labels"):
                yield Label("", id="db-label")
                yield Label("", id="status-bar")
        yield Rule(line_style="solid")

    def set_db(self, name: str, drop_nulls: bool = False, db_key: str = "") -> None:
        color = _DB_COLORS.get(db_key, "#D08A55")
        nulls_tag = "  [yellow]◈ NULOS OFF[/yellow]" if drop_nulls else ""
        self.query_one("#db-label", Label).update(
            f"[bold {color}]◆ {name.upper()}[/bold {color}]  [dim](^D para cambiar)[/dim]{nulls_tag}"
        )

    def set_status(self, msg: str) -> None:
        lower = msg.lower()
        if lower.startswith("error"):
            icon, color = "✗", "red"
        elif any(k in lower for k in ("ejecutando", "consultando", "guardando")):
            icon, color = "▷", "yellow"
        elif any(k in lower for k in ("guardado", "filas", "columnas", "caché", "activado", "desactivado", "→")):
            icon, color = "✓", "green"
        else:
            icon, color = "▸", "white"
        self.query_one("#status-bar", Label).update(f"[{color}]{icon}[/{color}] [dim]{msg}[/dim]")


class EditorPanel(Static):
    DEFAULT_QUERY = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-filebar"):
            yield Label("[yellow]● Sin guardar[/yellow]", id="save-status")
            yield Button("◆ Guardar (⌃S)", id="btn-save-adql", variant="default")
            yield Button("◈ Guardar como (⌃⇧S)", id="btn-save-as-adql", variant="default")
            yield Button("◇ Cargar (⌃O)", id="btn-load-adql", variant="default")
            yield Button("✚ Nueva (⌃T)", id="btn-new-adql", variant="default")
        yield ADQLTextArea(
            self.DEFAULT_QUERY,
            language="sql",
            theme="monokai",
            show_line_numbers=True,
            id="adql-editor",
        )
        with Horizontal(id="editor-toolbar"):
            yield Button("✓ Validar (⌃B)", id="btn-validate", variant="default")
            yield Button("▶ Previa 10 (⌃G)", id="btn-preview", variant="default", disabled=True)
            yield Button("⚡ Ejecutar (⌃R)", id="btn-execute", variant="primary")
            yield Button("■ Cancelar (Esc)", id="btn-cancel", variant="error", disabled=True)

    def set_query_text(self, text: str) -> None:
        self.query_one("#adql-editor", TextArea).text = text

    def set_save_state(self, path: Path | None, dirty: bool) -> None:
        status = self.query_one("#save-status", Label)
        if path is None:
            status.update("[yellow]● Sin guardar[/yellow]")
            return
        try:
            shown = str(path.relative_to(Path.cwd()))
        except ValueError:
            try:
                shown = "~/" + str(path.relative_to(Path.home()))
            except ValueError:
                shown = str(path)
        if dirty:
            status.update(f"[yellow]● Modificado[/yellow] [dim]→[/dim] [white]{shown}[/white]")
        else:
            status.update(f"[green]✓ Guardado[/green] [dim]→[/dim] [bold white]{shown}[/bold white]")


class HistoryListView(ListView):
    BINDINGS = [
        Binding("d", "act('delete')", "Borrar", show=False),
        Binding("r", "act('retry')", "Reintentar", show=False),
        Binding("k", "act('kill')", "Aniquilar", show=False),
        Binding("delete", "act('delete')", "Borrar", show=False),
    ]

    def action_act(self, what: str) -> None:
        item = self.highlighted_child
        if item is None or not item.id or not item.id.startswith("hq-"):
            return
        entry_id = item.id[3:]
        self.post_message(SidebarPanel.HistoryAction(what, entry_id))


class SidebarPanel(Static):
    """Right panel: current table + columns + query history with live status."""

    class TableSelected(Message):
        def __init__(self, table_name: str) -> None:
            super().__init__()
            self.table_name = table_name

    class HistoryAction(Message):
        def __init__(self, action: str, entry_id: str) -> None:
            super().__init__()
            self.action = action
            self.entry_id = entry_id

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._query_history: list[QueryHistoryEntry] = []
        self._cols_all: list[str] = []
        self._cols_selected: list[str] | None = None
        self._cols_schema_known: bool = True
        self._cols_filter: str = ""

    def compose(self) -> ComposeResult:
        yield Label("[bold]◈ TABLA:[/bold] [dim]—[/dim]", id="table-label")
        yield Rule(line_style="solid")
        yield Label("[bold]▾ Columnas:[/bold]", id="columns-title")
        yield Input(placeholder="filtrar columnas…", id="cols-filter")
        yield ListView(id="columns-list")
        yield Rule(line_style="dashed")
        yield Label("[bold]◎ Historial[/bold] [dim](d borrar · r reintentar · k aniquilar)[/dim]", id="history-title")
        yield HistoryListView(id="history-list")

    def get_entry(self, entry_id: str) -> QueryHistoryEntry | None:
        return next((e for e in self._query_history if e.id == entry_id), None)

    def remove_entry(self, entry_id: str) -> None:
        self._query_history = [e for e in self._query_history if e.id != entry_id]
        lv = self.query_one("#history-list", ListView)
        try:
            item = lv.get_child_by_id(f"hq-{entry_id}")
            item.remove()
        except Exception:
            pass

    def update_table(self, name: str) -> None:
        self.query_one("#table-label", Label).update(
            f"[bold]◈ TABLA:[/bold] [bright_blue]{name}[/bright_blue]"
        )

    def update_columns(
        self,
        cols: list[str],
        selected: list[str] | None = None,
        schema_known: bool = True,
    ) -> None:
        self._cols_all = list(cols)
        self._cols_selected = selected
        self._cols_schema_known = schema_known
        self._render_columns()

    def _render_columns(self) -> None:
        cols = self._cols_all
        selected = self._cols_selected
        schema_known = self._cols_schema_known
        flt = self._cols_filter.lower().strip()

        lv = self.query_one("#columns-list", ListView)
        lv.clear()

        schema_set: set[str] = set(cols)
        pinned: set[str] = set()

        def matches(name: str) -> bool:
            return not flt or flt in name.lower()

        if selected and selected != ["*"]:
            for sel in selected:
                pinned.add(sel)
                if not matches(sel):
                    continue
                if sel in schema_set:
                    lv.append(ListItem(Label(f"[bold green]● {sel}[/bold green]")))
                elif schema_known:
                    lv.append(ListItem(Label(f"[bold red]✗ {sel}[/bold red]")))
                else:
                    lv.append(ListItem(Label(f"[bold]◌ {sel}[/bold]")))
            if pinned and any(matches(s) for s in selected):
                lv.append(ListItem(Label("[dim]──────[/dim]")))

        rest = [c for c in cols if c not in pinned and matches(c)]
        for col in rest:
            lv.append(ListItem(Label(f"[dim]▸[/dim] [green]{col}[/green]")))

        if not cols and not selected:
            lv.append(ListItem(Label("[dim]· sin columnas[/dim]")))
        elif flt and not rest and not any(matches(s) for s in (selected or [])):
            lv.append(ListItem(Label(f"[dim]· sin coincidencias para '{flt}'[/dim]")))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "cols-filter":
            return
        self._cols_filter = event.value
        self._render_columns()

    @staticmethod
    def _snippet(adql: str) -> str:
        return re.sub(r"\s+", " ", adql).strip()[:60]

    @classmethod
    def _markup_for(cls, entry: QueryHistoryEntry) -> str:
        snip = cls._snippet(entry.adql)
        ts = entry.started_wall or ""
        ts_tag = f"[dim]{ts}[/dim] " if ts else ""
        if entry.status == "running":
            return f"{ts_tag}[yellow]●[/] {snip}  ⏱ {entry.elapsed:.1f}s"
        if entry.status == "ok":
            rc = entry.row_count if entry.row_count is not None else 0
            return f"{ts_tag}[green]●[/] {snip}  ✓ {rc} rows  ⏱ {entry.elapsed:.2f}s"
        if entry.status == "error":
            err = (entry.error or "")[:50]
            return f"{ts_tag}[red]●[/] {snip}  ✗ {err}  ⏱ {entry.elapsed:.2f}s"
        return f"{ts_tag}[red]●[/] {snip}  ⊘ cancelled  ⏱ {entry.elapsed:.1f}s"

    def upsert_entry(self, entry: QueryHistoryEntry) -> None:
        existing_idx = next(
            (i for i, e in enumerate(self._query_history) if e.id == entry.id),
            None,
        )
        lv = self.query_one("#history-list", ListView)
        item_id = f"hq-{entry.id}"
        markup = self._markup_for(entry)
        if existing_idx is None:
            self._query_history.insert(0, entry)
            new_item = ListItem(Static(markup, markup=True), id=item_id)
            if lv.children:
                lv.mount(new_item, before=lv.children[0])
            else:
                lv.mount(new_item)
            return
        if self._query_history[existing_idx] is not entry:
            self._query_history[existing_idx] = entry
        try:
            item = lv.get_child_by_id(item_id)
            item.query_one(Static).update(markup)
        except Exception:
            try:
                lv.mount(ListItem(Static(markup, markup=True), id=item_id))
            except Exception:
                pass

    def refresh_running(self) -> None:
        lv = self.query_one("#history-list", ListView)
        for i, e in enumerate(self._query_history):
            if e.status != "running":
                continue
            item_id = f"hq-{e.id}"
            try:
                item = lv.get_child_by_id(item_id)
            except Exception:
                continue
            try:
                static = item.query_one(Static)
                static.update(self._markup_for(e))
            except Exception:
                pass

    def _rerender(self) -> None:
        lv = self.query_one("#history-list", ListView)
        lv.clear()
        for e in self._query_history:
            lv.append(
                ListItem(Static(self._markup_for(e), markup=True), id=f"hq-{e.id}")
            )


class ResultsPanel(Static):
    def compose(self) -> ComposeResult:
        yield Label("[bold]▼ Resultados[/bold]", id="results-title")
        yield DataTable(id="results-table", zebra_stripes=True)

    def update_results(self, result: QueryResult) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear(columns=True)
        if result.df.empty:
            return
        table.add_columns(*[str(c) for c in result.df.columns])
        for row in result.df.itertuples(index=False):
            table.add_row(*[str(v) for v in row])


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


@dataclass
class QueryJob:
    entry: QueryHistoryEntry
    worker: Worker | None
    cancel_event: threading.Event
    response_holder: list
    save_target: tuple[Path, str, str] | None


class AstroDbApp(App):
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("ctrl+r", "run_query", "Ejecutar", priority=True, show=False),
        Binding("ctrl+b", "validate_adql", "Validar", priority=True, show=False),
        Binding("ctrl+g", "preview_query", "Previa", priority=True, show=False),
        Binding("ctrl+d", "cycle_db", "Cambiar DB", priority=True),
        Binding("ctrl+f", "fetch_columns", "Columnas", priority=True),
        Binding("ctrl+n", "toggle_nulls", "Nulos"),
        Binding("ctrl+s", "save_adql", "Guardar consulta", priority=True, show=False),
        Binding("ctrl+shift+s", "save_as_adql", "Guardar como", priority=True, show=False),
        Binding("ctrl+o", "load_adql", "Cargar consulta", priority=True, show=False),
        Binding("ctrl+t", "new_adql", "Nueva consulta", priority=True, show=False),
        Binding("ctrl+h", "toggle_results", "Ocultar vista", priority=True),
        Binding("escape", "cancel_query", "Cancelar", show=False),
        Binding("ctrl+q", "quit", "Salir"),
        Binding("ctrl+l", "restart", "Reiniciar"),
        Binding("tab", "cycle_focus(False)", show=False),
        Binding("shift+tab", "cycle_focus(True)", show=False),
    ]

    _FOCUS_RING = ("#adql-editor", "#results-table", "#cols-filter", "#columns-list", "#history-list")

    _db_index: int = 0
    _drop_nulls: bool = False
    _flow_state: Literal["dirty", "validated", "previewed"] = "dirty"
    _last_result: QueryResult | None = None
    _schema_cache: dict[str, list[str]]
    _clients: dict[str, HTTPQueryClient]
    _last_detected_table: str | None = None
    _save_path: Path | None = None
    _save_dirty: bool = True

    def __init__(self) -> None:
        super().__init__()
        self._clients = {}
        self._schema_cache = {}
        self._last_detected_table = None
        self._save_path = None
        self._save_dirty = True
        self._jobs: dict[str, QueryJob] = {}
        self._query_history: list[QueryHistoryEntry] = []
        self._db_locks: dict[Path, asyncio.Lock] = {}

    @property
    def _current_db(self) -> DatabaseConfig:
        return DATABASES[self._db_index]

    @property
    def _client(self) -> HTTPQueryClient:
        name = self._current_db.name
        if name not in self._clients:
            db = self._current_db
            self._clients[name] = HTTPQueryClient(db.query_url, db.columns_mode)
        return self._clients[name]

    def compose(self) -> ComposeResult:
        with Vertical(id="app-container"):
            yield HeaderPanel(id="header-panel")
            with Horizontal(id="main-layout"):
                with Vertical(id="left-panel"):
                    yield EditorPanel(id="editor-container")
                    yield ResultsPanel(id="results-container", classes="hidden")
                yield SidebarPanel(id="sidebar-container")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(HeaderPanel).set_db(
            self._current_db.display_name, db_key=self._current_db.name
        )
        self.set_interval(0.2, self._tick_running)

    def _tick_running(self) -> None:
        try:
            self.query_one(SidebarPanel).refresh_running()
        except Exception:
            pass

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        table = _parse_table(text)
        if table:
            self.query_one(SidebarPanel).update_table(table)
            if table in self._schema_cache:
                self._refresh_sidebar_columns(table, text)
            self._last_detected_table = table
        self._set_flow_state("dirty")
        if not self._save_dirty:
            self._save_dirty = True
            self.query_one(EditorPanel).set_save_state(self._save_path, True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-validate":
            self.action_validate_adql()
        elif bid == "btn-preview":
            self.action_preview_query()
        elif bid == "btn-execute":
            self.action_run_query()
        elif bid == "btn-cancel":
            self.action_cancel_query()
        elif bid == "btn-save-adql":
            self.action_save_adql()
        elif bid == "btn-save-as-adql":
            self.action_save_as_adql()
        elif bid == "btn-load-adql":
            self.action_load_adql()
        elif bid == "btn-new-adql":
            self.action_new_adql()
        else:
            return

    def on_sidebar_panel_history_action(self, event: SidebarPanel.HistoryAction) -> None:
        sidebar = self.query_one(SidebarPanel)
        entry = sidebar.get_entry(event.entry_id)
        if entry is None:
            return
        if event.action == "delete":
            if entry.status == "running":
                self._set_status("No se puede borrar: en ejecución (k para aniquilar)")
                return
            sidebar.remove_entry(event.entry_id)
            self._query_history = [e for e in self._query_history if e.id != event.entry_id]
            self._set_status("Entrada borrada")
        elif event.action == "kill":
            if entry.status != "running":
                self._set_status("No está en ejecución")
                return
            self._cancel_job(event.entry_id)
            self._set_status("Aniquilando…")
        elif event.action == "retry":
            self.query_one(EditorPanel).set_query_text(entry.adql)
            self._set_flow_state("dirty")
            default_name = f"query_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.push_screen(
                ExportResultsModal(default_name=default_name, default_dir=Path.cwd()),
                self._on_save_target_chosen_for(entry.adql),
            )

    def on_sidebar_panel_table_selected(self, event: SidebarPanel.TableSelected) -> None:
        table = event.table_name
        sidebar = self.query_one(SidebarPanel)
        sidebar.update_table(table)
        if table in self._schema_cache:
            editor_text = self.query_one("#adql-editor", TextArea).text
            self._refresh_sidebar_columns(table, editor_text)
            self._set_status(f"{table} — {len(self._schema_cache[table])} columnas (caché)")
        else:
            self._do_fetch_columns(table)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cycle_focus(self, reverse: bool = False) -> None:
        focused = self.focused
        focused_id = f"#{focused.id}" if focused and focused.id else None
        results_visible = not self.query_one("#results-container").has_class("hidden")
        ring = tuple(
            sel for sel in self._FOCUS_RING
            if sel != "#results-table" or results_visible
        )
        idx = ring.index(focused_id) if focused_id in ring else -1
        step = -1 if reverse else 1
        next_idx = (idx + step) % len(ring)
        self.query_one(ring[next_idx]).focus()

    def action_cycle_db(self) -> None:
        self._db_index = (self._db_index + 1) % len(DATABASES)
        self.query_one(HeaderPanel).set_db(
            self._current_db.display_name, self._drop_nulls, db_key=self._current_db.name
        )
        self._set_status(f"→ {self._current_db.display_name} · ^F para columnas")
        self._set_flow_state("dirty")
        self._schema_cache.clear()
        self._last_detected_table = None
        self.query_one(SidebarPanel).update_columns([], None, False)

    def action_toggle_nulls(self) -> None:
        self._drop_nulls = not self._drop_nulls
        self.query_one(HeaderPanel).set_db(
            self._current_db.display_name, self._drop_nulls, db_key=self._current_db.name
        )
        label = "activado" if self._drop_nulls else "desactivado"
        self._set_status(f"Limpieza de nulos {label}")

    def action_run_query(self) -> None:
        adql = self.query_one("#adql-editor", TextArea).text
        err = _validate_local(adql)
        if err:
            self._set_status(f"Sintaxis: {err}")
            return
        default_name = f"query_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.push_screen(
            ExportResultsModal(default_name=default_name, default_dir=Path.cwd()),
            self._on_save_target_chosen_for(adql),
        )

    def _on_save_target_chosen_for(self, adql: str):
        def _cb(choice: tuple[Path, Literal["csv", "db"], str] | None) -> None:
            if choice is None:
                self._set_status("Cancelado")
                return
            path, fmt, table_name = choice
            self._launch_query(adql, save_target=(path, fmt, table_name))
        return _cb

    def action_validate_adql(self) -> None:
        adql = self.query_one("#adql-editor", TextArea).text
        err = _validate_local(adql)
        if err:
            self._set_status(f"Sintaxis: {err}")
            return
        self._set_status("Validando con servidor…")
        self._do_validate_remote(adql)

    def action_preview_query(self) -> None:
        if self._flow_state == "dirty":
            self._set_status("Validá primero (⌃B)")
            return
        adql = self.query_one("#adql-editor", TextArea).text
        self._launch_query(_inject_top(adql, 10), save_target=None, is_preview=True)

    def action_fetch_columns(self) -> None:
        adql = self.query_one("#adql-editor", TextArea).text
        table = _parse_table(adql)
        if not table:
            self.notify("Sin FROM en editor. Escribí 'FROM tabla' primero.", severity="warning", timeout=4)
            self._set_status("No se detectó tabla en FROM")
            return
        if table in self._schema_cache and self._schema_cache[table]:
            self._refresh_sidebar_columns(table, adql)
            self.notify(f"{table}: {len(self._schema_cache[table])} columnas (caché)", timeout=2)
            self._set_status(f"{len(self._schema_cache[table])} columnas (caché)")
            return
        self.notify(f"Consultando columnas de {table}…", timeout=2)
        self._do_fetch_columns(table)

    def action_cancel_query(self) -> None:
        n = len(self._jobs)
        if n == 0:
            self.action_close_preview()
            return
        if n == 1:
            job_id = next(iter(self._jobs.keys()))
            self._cancel_job(job_id)
            return
        items: list[tuple[str, str, float]] = []
        now = time.monotonic()
        for jid, job in self._jobs.items():
            snippet = re.sub(r"\s+", " ", job.entry.adql).strip()[:60]
            elapsed = now - job.entry.started_at
            items.append((jid, snippet, elapsed))
        self.push_screen(CancelPickerModal(items), self._on_cancel_picked)

    def _on_cancel_picked(self, job_id: str | None) -> None:
        if job_id is None:
            return
        self._cancel_job(job_id)

    def _cancel_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.cancel_event.set()
        if job.response_holder:
            try:
                job.response_holder[0].close()
            except Exception:
                pass
        if job.worker is not None:
            job.worker.cancel()

    def action_save_adql(self) -> None:
        if self._save_path is None:
            default_name = "query"
            self.push_screen(SaveQueryModal(default_name), self._on_save_path_selected)
            return
        self._do_save_adql(self._save_path)

    def action_save_as_adql(self) -> None:
        default_name = self._save_path.stem if self._save_path else "query"
        self.push_screen(SaveQueryModal(default_name), self._on_save_path_selected)

    def _on_save_path_selected(self, chosen: Path | None) -> None:
        if chosen is None:
            return
        self._do_save_adql(chosen)

    def _do_save_adql(self, path: Path) -> None:
        adql = self.query_one("#adql-editor", TextArea).text
        try:
            saved = AdqlStorage().save(adql, path)
            self._save_path = saved
            self._save_dirty = False
            self.query_one(EditorPanel).set_save_state(saved, False)
            self._set_status(f"Consulta guardada: {saved}")
        except Exception as exc:
            self._set_status(f"Error al guardar: {exc}")

    def action_new_adql(self) -> None:
        editor = self.query_one(EditorPanel)
        editor.set_query_text("")
        self._save_path = None
        self._save_dirty = True
        editor.set_save_state(None, True)
        self._set_flow_state("dirty")
        self._set_status("Nueva consulta")

    def action_load_adql(self) -> None:
        self.push_screen(LoadAdqlModal(), self._on_load_adql_selected)

    def _on_load_adql_selected(self, selected: Path | None) -> None:
        if selected is None:
            return
        try:
            text = AdqlStorage().load(selected)
        except Exception as exc:
            self._set_status(f"Error al cargar: {exc}")
            return
        editor = self.query_one(EditorPanel)
        editor.set_query_text(text)
        self._save_path = selected
        self._save_dirty = False
        editor.set_save_state(selected, False)
        self._set_status(f"Consulta cargada: {selected}")

    def action_toggle_results(self) -> None:
        panel = self.query_one("#results-container")
        if panel.has_class("hidden"):
            if self._last_result is None:
                self._set_status("Sin resultados para mostrar")
                return
            panel.remove_class("hidden")
            self._set_status("Vista mostrada")
        else:
            panel.add_class("hidden")
            self.query_one("#adql-editor").focus()
            self._set_status("Vista oculta")

    def action_close_preview(self) -> None:
        panel = self.query_one("#results-container")
        if not panel.has_class("hidden"):
            panel.add_class("hidden")
            self.query_one("#adql-editor").focus()
            self._set_status("Vista oculta")

    def action_restart(self) -> None:
        self.exit()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ------------------------------------------------------------------
    # Workers (thread)
    # ------------------------------------------------------------------

    def _launch_query(
        self,
        adql: str,
        save_target: tuple[Path, str, str] | None,
        is_preview: bool = False,
    ) -> str:
        entry = QueryHistoryEntry(
            id=uuid4().hex,
            adql=adql,
            started_at=time.monotonic(),
            started_wall=datetime.now().strftime("%H:%M:%S"),
        )
        self._query_history.append(entry)
        sidebar = self.query_one(SidebarPanel)
        sidebar.upsert_entry(entry)

        cancel_event = threading.Event()
        response_holder: list = []
        client = self._client

        async def runner() -> None:
            await self._run_job(entry, save_target, cancel_event, response_holder, client, is_preview)

        worker = self.run_worker(runner(), exclusive=False, group="queries")
        self._jobs[entry.id] = QueryJob(
            entry=entry,
            worker=worker,
            cancel_event=cancel_event,
            response_holder=response_holder,
            save_target=save_target,
        )
        self._update_running_badge()
        return entry.id

    async def _run_job(
        self,
        entry: QueryHistoryEntry,
        save_target: tuple[Path, str, str] | None,
        cancel_event: threading.Event,
        response_holder: list,
        client: HTTPQueryClient,
        is_preview: bool,
    ) -> None:
        try:
            result = await asyncio.to_thread(
                client.execute, entry.adql, cancel_event, response_holder
            )
            if self._drop_nulls:
                result = QueryResult(df=result.df.dropna())
            entry.row_count = len(result.df)
            self._last_result = result

            if save_target is not None:
                path, fmt, table_name = save_target
                if fmt == "csv":
                    await asyncio.to_thread(CSVStorage().save, result.df, path)
                else:
                    lock = self._db_locks.setdefault(path, asyncio.Lock())
                    async with lock:
                        await asyncio.to_thread(
                            SQLiteStorage().save, result.df, path, table_name
                        )
                entry.save_target = path

            entry.status = "ok"
            try:
                self._show_results(result)
            except Exception:
                pass

            table = _parse_table(entry.adql)
            if table:
                self._schema_cache[table] = result.columns
                try:
                    self._update_sidebar_after_query(table, entry.adql, result.columns)
                except Exception:
                    pass
            if is_preview:
                self._set_flow_state("previewed")
        except QueryCancelled:
            entry.status = "cancelled"
        except asyncio.CancelledError:
            cancel_event.set()
            if response_holder:
                try:
                    response_holder[0].close()
                except Exception:
                    pass
            entry.status = "cancelled"
            raise
        except TransientServerError as exc:
            entry.status = "error"
            entry.error = f"Servidor TAP caído: {exc}"[:120]
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)[:120]
        finally:
            entry.finished_at = time.monotonic()
            try:
                self.query_one(SidebarPanel).upsert_entry(entry)
            except Exception:
                pass
            self._jobs.pop(entry.id, None)
            self._update_running_badge()

    @work(thread=True)
    def _do_validate_remote(self, adql: str) -> None:
        try:
            self._client.execute(_inject_top(adql, 0))
            self.call_from_thread(self._set_flow_state, "validated")
            self.call_from_thread(self._set_status, "ADQL válido")
        except TransientServerError as exc:
            self.call_from_thread(self._set_flow_state, "validated")
            self.call_from_thread(
                self._set_status,
                f"Servidor TAP caído ({exc}) — previa habilitada igual",
            )
        except ValueError as exc:
            self.call_from_thread(self._set_status, f"Sintaxis ADQL: {exc}")
        except Exception as exc:
            self.call_from_thread(self._set_status, f"Error: {exc}")

    @work(thread=True)
    def _do_fetch_columns(self, table: str) -> None:
        self.call_from_thread(self._set_status, f"Consultando columnas de {table}...")
        try:
            cols = self._client.get_columns(table)
            self._schema_cache[table] = cols
            editor_text = self.query_one("#adql-editor", TextArea).text
            self.call_from_thread(self._update_sidebar_after_query, table, editor_text, cols)
            self.call_from_thread(
                self._set_status,
                f"{len(cols)} columnas" if cols else "Sin resultados en TAP_SCHEMA",
            )
        except TransientServerError as exc:
            self.call_from_thread(self._set_status, f"Servidor TAP caído ({exc})")
        except Exception as exc:
            self.call_from_thread(self._set_status, f"Error: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_sidebar_columns(self, table: str, adql: str) -> None:
        selected = _parse_select_columns(adql)
        cols = self._schema_cache.get(table, [])
        schema_known = table in self._schema_cache
        self.query_one(SidebarPanel).update_columns(cols, selected, schema_known)

    def _set_status(self, msg: str) -> None:
        self.query_one(HeaderPanel).set_status(msg)

    def _update_running_badge(self) -> None:
        n = sum(1 for j in self._jobs.values() if j.entry.status == "running")
        if n > 0:
            self._set_status(f"▶ {n} en ejecución")
        else:
            self._set_status("Listo")
        self._update_cancel_button()

    def _update_cancel_button(self) -> None:
        try:
            btn = self.query_one("#btn-cancel", Button)
            btn.disabled = (len(self._jobs) == 0)
        except Exception:
            pass

    def _show_results(self, result: QueryResult) -> None:
        panel = self.query_one("#results-container", ResultsPanel)
        panel.remove_class("hidden")
        panel.update_results(result)

    def _update_sidebar_after_query(self, table: str, adql: str, cols: list[str]) -> None:
        sidebar = self.query_one(SidebarPanel)
        sidebar.update_table(table)
        self._refresh_sidebar_columns(table, adql)

    def _set_flow_state(self, state: str) -> None:
        self._flow_state = state  # type: ignore[assignment]
        try:
            preview_btn = self.query_one("#btn-preview", Button)
            preview_btn.disabled = (state == "dirty")
            exec_btn = self.query_one("#btn-execute", Button)
            exec_btn.variant = "success" if state == "previewed" else "primary"
        except Exception:
            pass


def main() -> None:
    AstroDbApp().run()


if __name__ == "__main__":
    main()
