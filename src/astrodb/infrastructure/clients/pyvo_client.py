from __future__ import annotations
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from io import StringIO

import pandas as pd

from astrodb.domain.models import QueryResult
from astrodb.domain.schemas import KNOWN_COLUMNS

_HEADERS = {"User-Agent": "astrodb-tui/0.1"}
_RETRY_DELAYS = (1.0, 2.0)


class TransientServerError(RuntimeError):
    """5xx, timeout, o conexión caída — no es problema de sintaxis."""


class QueryCancelled(Exception):
    """Raised when an in-flight query is cancelled via cancel_event."""


class HTTPQueryClient:
    """Generic REST client for astronomical DB endpoints that return CSV.

    All three target services expose a URL of the form ``{base_url}{query}``
    where the query string is URL-encoded.  SDSS uses SQL Server dialect;
    VizieR and NASA Exoplanet use ADQL via TAP sync.
    """

    def __init__(self, query_url: str, columns_mode: str = "tap_schema") -> None:
        self._query_url = query_url
        # "tap_schema" → query TAP_SCHEMA.columns
        # "select_top1" → SELECT TOP 1 * and take column names from result
        self._columns_mode = columns_mode

    def execute(
        self,
        query: str,
        cancel_event: threading.Event | None = None,
        response_holder: list | None = None,
    ) -> QueryResult:
        url = self._query_url + urllib.parse.quote(query, safe="")
        req = urllib.request.Request(url, headers=_HEADERS)
        attempts = len(_RETRY_DELAYS) + 1
        last_transient: Exception | None = None
        for i in range(attempts):
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelled()
            try:
                resp = urllib.request.urlopen(req, timeout=60)
                if response_holder is not None:
                    response_holder.append(resp)
                try:
                    if cancel_event is not None and cancel_event.is_set():
                        raise QueryCancelled()
                    content = resp.read().decode("utf-8")
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                break
            except QueryCancelled:
                raise
            except urllib.error.HTTPError as exc:
                if 500 <= exc.code < 600:
                    last_transient = exc
                    if i < attempts - 1:
                        time.sleep(_RETRY_DELAYS[i])
                        continue
                    raise TransientServerError(f"HTTP {exc.code} tras {attempts} intentos") from exc
                # 4xx → error del cliente (sintaxis ADQL)
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                detail = f": {body.strip()}" if body.strip() else ""
                raise ValueError(f"HTTP {exc.code}{detail}") from exc
            except urllib.error.URLError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise QueryCancelled() from exc
                last_transient = exc
                if i < attempts - 1:
                    time.sleep(_RETRY_DELAYS[i])
                    continue
                raise TransientServerError(f"Sin conexión: {exc.reason}") from exc
            except Exception as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise QueryCancelled() from exc
                raise
        else:
            raise TransientServerError(str(last_transient))
        try:
            df = pd.read_csv(StringIO(content), comment="#")
        except Exception as exc:
            raise ValueError(f"Respuesta no parseable como CSV: {content[:300]}") from exc
        return QueryResult(df=df)

    def get_columns(self, table_name: str) -> list[str]:
        if table_name in KNOWN_COLUMNS:
            return KNOWN_COLUMNS[table_name]
        if self._columns_mode == "tap_schema":
            try:
                # VizieR stores table names with literal quotes → use LIKE
                leaf = table_name.split("/")[-1]
                query = (
                    f"SELECT column_name FROM TAP_SCHEMA.columns "
                    f"WHERE table_name LIKE '%{leaf}%'"
                )
                result = self.execute(query)
                if "column_name" in result.df.columns:
                    cols = [c.strip("'") for c in result.df["column_name"].dropna().tolist()]
                    if cols:
                        return cols
            except ValueError:
                pass
            # TAP_SCHEMA failed or empty — fall back to select_top1
        quoted = f'"{table_name}"' if "/" in table_name else table_name
        result = self.execute(f"SELECT TOP 1 * FROM {quoted}")
        return list(result.df.columns)
