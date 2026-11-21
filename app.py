from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
from nicegui import ui

DB_PATH = Path(__file__).with_name("file.duckdb")

# Connect to DuckDB
con = duckdb.connect(str(DB_PATH))



GEBIETE = ["USA", "AD Baden", "AD Bayern", "AD Bayern Dienst", "AD Bayern Porto", "AD Bergedorf", "AD Bremen", "AD Braunschweig", "AD Hamburg", "Südafrika"]
SPEZIAL_GEBIETE = ["AM-POST Am.Druck Plattenfehler"]
MAX_KATALOGNUMMER = 2_147_483_647

def initialize_stamps_table():
    """Create the stamps table if it doesn't exist."""
    # Create sequence if it doesn't exist
    con.execute("CREATE SEQUENCE IF NOT EXISTS stamp_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS stamps (
            id INTEGER PRIMARY KEY DEFAULT nextval('stamp_id_seq'),
            gebiet VARCHAR,
            katalognummer INTEGER NOT NULL,
            entwertung VARCHAR,
            besitz INTEGER,
            variante VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Older databases stored besitz as text; convert it so the column is numeric.
    besitz_type = con.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'stamps' AND column_name = 'besitz'
    """).fetchone()
    if besitz_type and besitz_type[0] != "INTEGER":
        con.execute("ALTER TABLE stamps ALTER COLUMN besitz SET DATA TYPE INTEGER")
        con.execute("CHECKPOINT")
    katalognummer_type = con.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'stamps' AND column_name = 'katalognummer'
    """).fetchone()
    if katalognummer_type and katalognummer_type[0] != "INTEGER":
        invalid_count = con.execute("""
            SELECT COUNT(*) FROM stamps
            WHERE TRY_CAST(katalognummer AS INTEGER) IS NULL
        """).fetchone()[0]
        if invalid_count:
            raise ValueError(
                f"{invalid_count} Katalognummer(n) können nicht als INTEGER gespeichert werden."
            )
        con.execute("ALTER TABLE stamps ALTER COLUMN katalognummer SET DATA TYPE INTEGER")
        con.execute("CHECKPOINT")


def initialize_special_stamps_table():
    """Create the special stamps table if it doesn't exist."""
    con.execute("CREATE SEQUENCE IF NOT EXISTS special_stamp_id_seq START 1")
    con.execute("""
        CREATE TABLE IF NOT EXISTS special_stamps (
            id INTEGER PRIMARY KEY DEFAULT nextval('special_stamp_id_seq'),
            gebiet VARCHAR,
            katalognummer INTEGER NOT NULL,
            entwertung VARCHAR,
            besitz INTEGER,
            variante VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            typ VARCHAR
        )
    """)


def _stamp_table(special: bool) -> str:
    return "special_stamps" if special else "stamps"


def _configured_gebiete(special: bool) -> list[str]:
    return SPEZIAL_GEBIETE if special else GEBIETE


def add_stamp(gebiet: str, katalognummer: int, entwertung: str,
              besitz: Optional[int] = None, variante: Optional[str] = None,
              *, special: bool = False, typ: Optional[str] = None) -> int:
    """
    Add a postage stamp to the database.
    
    Args:
        gebiet: Region/area (optional)
        katalognummer: Catalog number (required)
        entwertung: Cancellation/usage status (optional)
        besitz: Ownership/possession status (required)
        variante: Stamp variant (optional)
    
    Returns:
        The ID of the newly inserted stamp
    """
    columns = ", typ" if special else ""
    placeholder = ", ?" if special else ""
    params = [gebiet, katalognummer, entwertung, besitz, variante]
    if special:
        params.append(typ)
    result = con.execute(f"""
        INSERT INTO {_stamp_table(special)} (gebiet, katalognummer, entwertung, besitz, variante{columns})
        VALUES (?, ?, ?, ?, ?{placeholder})
        RETURNING id
    """, params).fetchall()
    
    return result[0][0] if result else None


def add_stamp_range(
    gebiet: str,
    lower: int,
    upper: int,
    entwertung: str,
    besitz: int,
    variante: str,
    *,
    special: bool = False,
    typ: Optional[str] = None,
) -> int:
    """Insert a catalog-number range, skipping numbers already present in the Gebiet."""
    columns = ", typ" if special else ""
    placeholder = ", ?" if special else ""
    params = [gebiet, entwertung, besitz, variante]
    if special:
        params.append(typ)
    params.extend([lower, upper + 1, gebiet])
    table = _stamp_table(special)
    rows = con.execute(f"""
        INSERT INTO {table} (gebiet, katalognummer, entwertung, besitz, variante{columns})
        SELECT ?, catalog_range.number, ?, ?, ?{placeholder}
        FROM range(?, ?) AS catalog_range(number)
        WHERE NOT EXISTS (
            SELECT 1 FROM {table} existing
            WHERE existing.gebiet = ? AND existing.katalognummer = catalog_range.number
        )
        RETURNING id
    """, params).fetchall()
    return len(rows)


def _query_dicts(query: str, params: Optional[list] = None) -> list[dict]:
    """Run a query and return the rows as dictionaries keyed by column name."""
    cursor = con.execute(query, params or [])
    columns = [col[0] for col in cursor.description]
    rows = []
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        rows.append({k: (str(v) if isinstance(v, datetime) else v) for k, v in row.items()})
    return rows


def get_all_stamps() -> list[dict]:
    """Retrieve all stamps from the database, ordered by catalog number."""
    return _query_dicts("SELECT * FROM stamps ORDER BY katalognummer ASC, id ASC")


ALL_GEBIETE = "Alle"


def get_gebiete(*, special: bool = False) -> list[str]:
    """Return the distinct, non-empty Gebiet values in alphabetical order."""
    rows = con.execute(
        f"SELECT DISTINCT gebiet FROM {_stamp_table(special)} "
        "WHERE gebiet IS NOT NULL AND gebiet <> '' ORDER BY gebiet"
    ).fetchall()
    return [row[0] for row in rows]


def get_owned_stamp_counts(*, special: bool = False) -> dict[str, int]:
    """Return the number of stamps in possession for each configured Gebiet."""
    gebiete = _configured_gebiete(special)
    counts = dict.fromkeys(gebiete, 0)
    if not gebiete:
        return counts
    placeholders = ", ".join("?" for _ in gebiete)
    rows = con.execute(f"""
        SELECT gebiet, COUNT(*) AS anzahl
        FROM {_stamp_table(special)}
        WHERE besitz > 0 AND gebiet IN ({placeholders})
        GROUP BY gebiet
    """, gebiete).fetchall()
    counts.update({gebiet: anzahl for gebiet, anzahl in rows})
    return counts


def get_stamps_by_gebiet(gebiet: str, katalognummer_filter: str = "", *, special: bool = False) -> list[dict]:
    """Return stamps filtered by Gebiet and a partial catalog-number match."""
    query = f"SELECT * FROM {_stamp_table(special)}"
    conditions = []
    params = []
    if gebiet != ALL_GEBIETE:
        conditions.append("gebiet = ?")
        params.append(gebiet)
    if katalognummer_filter:
        conditions.append("CAST(katalognummer AS VARCHAR) LIKE ?")
        params.append(f"%{katalognummer_filter}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    return _query_dicts(query + " ORDER BY katalognummer ASC, id ASC", params)


def get_missing_stamps(gebiet: str, *, special: bool = False) -> list[dict]:
    """Return stamps with no copies in possession, optionally filtered by Gebiet."""
    query = f"SELECT * FROM {_stamp_table(special)} WHERE besitz = 0"
    params = []
    if gebiet != ALL_GEBIETE:
        query += " AND gebiet = ?"
        params.append(gebiet)
    return _query_dicts(query + " ORDER BY katalognummer ASC, id ASC", params)


def get_collection_stats(gebiet: str, *, special: bool = False) -> list[dict]:
    """Per Entwertung: stamps owned (besitz > 0), all stamps and the owned share in percent."""
    query = f"""
        SELECT COALESCE(NULLIF(entwertung, ''), '(ohne Angabe)') AS entwertung,
               COUNT(*) AS gesamt,
               COUNT(*) FILTER (WHERE besitz > 0) AS besitz
        FROM {_stamp_table(special)}
    """
    params = []
    if gebiet != ALL_GEBIETE:
        query += " WHERE gebiet = ?"
        params.append(gebiet)
    rows = _query_dicts(query + " GROUP BY 1 ORDER BY 1", params)
    for row in rows:
        row["anteil"] = round(row["besitz"] / row["gesamt"] * 100, 1) if row["gesamt"] else 0
    return rows


def get_stamp_by_katalognummer(katalognummer: str):
    """Retrieve stamps by catalog number."""
    return con.execute(
        "SELECT * FROM stamps WHERE katalognummer = ?", 
        [katalognummer]
    ).fetchall()


def delete_stamp(stamp_id: int, *, special: bool = False) -> bool:
    """Delete a stamp by ID."""
    return con.execute(f"DELETE FROM {_stamp_table(special)} WHERE id = ? RETURNING id", [stamp_id]).fetchone() is not None


def update_stamp(stamp_id: int, gebiet: Optional[str] = None, katalognummer: Optional[int] = None,
                 entwertung: Optional[str] = None, besitz: Optional[int] = None,
                 variante: Optional[str] = None, *, special: bool = False,
                 typ: Optional[str] = None) -> bool:
    """Update an existing stamp. Only provided fields are updated."""
    updates = []
    params = []
    
    if gebiet is not None:
        updates.append("gebiet = ?")
        params.append(gebiet)
    if katalognummer is not None:
        updates.append("katalognummer = ?")
        params.append(katalognummer)
    if entwertung is not None:
        updates.append("entwertung = ?")
        params.append(entwertung)
    if besitz is not None:
        updates.append("besitz = ?")
        params.append(besitz)
    if variante is not None:
        updates.append("variante = ?")
        params.append(variante)
    if special and typ is not None:
        updates.append("typ = ?")
        params.append(typ)
    
    if not updates:
        return False
    
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(stamp_id)
    
    query = f"UPDATE {_stamp_table(special)} SET {', '.join(updates)} WHERE id = ?"
    return con.execute(query + " RETURNING id", params).fetchone() is not None

FIELD_LABELS = {
    "gebiet": "Gebiet",
    "katalognummer": "Katalognummer *",
    "entwertung": "Entwertung",
    "besitz": "Besitz",
    "variante": "Variante",
}

STAT_COLUMNS = [
    {"name": "entwertung", "label": "Entwertung", "field": "entwertung", "align": "left"},
    {"name": "besitz", "label": "Besitz", "field": "besitz", "align": "right"},
    {"name": "gesamt", "label": "Gesamt", "field": "gesamt", "align": "right"},
    {"name": "anteil", "label": "Anteil in %", "field": "anteil", "align": "right"},
]

TABLE_COLUMNS = [
    {"name": "gebiet", "label": "Gebiet", "field": "gebiet", "sortable": True, "align": "left"},
    {"name": "katalognummer", "label": "Katalognummer", "field": "katalognummer", "sortable": True, "align": "left"},
    {"name": "entwertung", "label": "Entwertung", "field": "entwertung", "sortable": True, "align": "left"},
    {"name": "besitz", "label": "Besitz", "field": "besitz", "sortable": True, "align": "left"},
    {"name": "variante", "label": "Variante", "field": "variante", "sortable": True, "align": "left"},
    {"name": "actions", "label": "", "field": "id", "align": "right"},
]
TYP_COLUMN = {"name": "typ", "label": "Typ", "field": "typ", "sortable": True, "align": "left"}


def stamp_form(special: bool = False) -> dict[str, ui.input | ui.number | ui.select]:
    """Create the input fields for a stamp and return them keyed by column name."""
    with ui.grid(columns=2).classes("w-full"):
        fields = {}
        labels = {**FIELD_LABELS, "typ": "Typ"} if special else FIELD_LABELS
        for name, label in labels.items():
            if name in {"besitz", "katalognummer"}:
                fields[name] = ui.number(label, min=0, precision=0).classes("w-full")
            elif name == "gebiet":
                fields[name] = ui.select(_configured_gebiete(special), label=label).classes("w-full")
            else:
                fields[name] = ui.input(label).classes("w-full")
        return fields


def read_form(form: dict[str, ui.input | ui.number | ui.select], *, special: bool = False,
              existing_gebiet: Optional[str] = None) -> Optional[dict]:
    """Return the form values (besitz as int, empty means 0), or None if invalid."""
    values = {
        name: inp.value if isinstance(inp, (ui.number, ui.select)) else (inp.value or "").strip()
        for name, inp in form.items()
    }
    if values["gebiet"] not in _configured_gebiete(special) and not (
        special and existing_gebiet and values["gebiet"] == existing_gebiet
    ):
        ui.notify("Bitte ein Gebiet aus der Liste wählen.", type="negative")
        return None
    katalognummer = values["katalognummer"]
    if katalognummer is None or katalognummer == "":
        ui.notify("Katalognummer ist ein Pflichtfeld.", type="negative")
        return None
    try:
        katalognummer = int(katalognummer)
    except (TypeError, ValueError):
        ui.notify("Die Katalognummer muss eine ganze Zahl sein.", type="negative")
        return None
    if not 0 <= katalognummer <= MAX_KATALOGNUMMER:
        ui.notify("Die Katalognummer muss zwischen 0 und 2147483647 liegen.", type="negative")
        return None
    values["katalognummer"] = katalognummer
    values["besitz"] = int(values["besitz"] or 0)
    return values


def page_navigation(special: bool):
    prefix = "/spezial" if special else ""
    with ui.row().classes("w-full items-center gap-4"):
        ui.link("Sammlung wählen", "/")
        ui.link("Eingabe", f"{prefix}/eingabe")
        ui.link("Bereichseingabe", f"{prefix}/bereich")
        ui.link("Abfrage", f"{prefix}/abfrage")
        ui.link("Fehlliste", f"{prefix}/fehlliste")


@ui.page("/")
def start_page():
    ui.label("Briefmarkensammlung").classes("text-h4")
    ui.label("Sammlung wählen").classes("text-h6")
    with ui.row().classes("gap-4"):
        ui.link("Allgemeine Sammlung", "/eingabe")
        ui.link("Spezialsammlungen", "/spezial/eingabe")


def collection_title(special: bool):
    ui.label("Spezialsammlungen" if special else "Briefmarkensammlung").classes("text-h4")


@ui.page("/eingabe")
def input_page():
    build_input_page(False)


@ui.page("/spezial/eingabe")
def special_input_page():
    build_input_page(True)


def build_input_page(special: bool):
    page_navigation(special)
    collection_title(special)

    with ui.card().classes("w-full"):
        ui.label("Neue Briefmarke erfassen").classes("text-h6")
        add_form = stamp_form(special)

        def save_new():
            data = read_form(add_form, special=special)
            if data is None:
                return
            stamp_id = add_stamp(**data, special=special)
            ui.notify(f"Briefmarke mit ID {stamp_id} gespeichert.", type="positive")
            refresh_owned_counts()

        with ui.row().classes("justify-end w-full"):
            ui.button("Speichern", icon="save", on_click=save_new)

        for field in add_form.values():
            if not isinstance(field, ui.select):
                field.on("keydown.enter", save_new)

    with ui.card().classes("w-full"):
        ui.label("Besitz je Gebiet").classes("text-h6")
        with ui.grid(columns=2).classes("w-full"):
            owned_count_labels = {
                gebiet: ui.label().classes("text-body1")
                for gebiet in _configured_gebiete(special)
            }

    def refresh_owned_counts():
        for gebiet, count in get_owned_stamp_counts(special=special).items():
            owned_count_labels[gebiet].text = f"{gebiet}: {count} Marken im Besitz"

    refresh_owned_counts()


@ui.page("/bereich")
def range_input_page():
    build_range_input_page(False)


@ui.page("/spezial/bereich")
def special_range_input_page():
    build_range_input_page(True)


def build_range_input_page(special: bool):
    page_navigation(special)
    collection_title(special)

    with ui.card().classes("w-full"):
        ui.label("Mehrere Briefmarken erfassen").classes("text-h6")
        lower_field = ui.number("Katalognummer von *", min=0, max=MAX_KATALOGNUMMER, precision=0).classes("w-full")
        upper_field = ui.number("Katalognummer bis *", min=0, max=MAX_KATALOGNUMMER, precision=0).classes("w-full")
        with ui.grid(columns=2).classes("w-full"):
            gebiet_field = ui.select(_configured_gebiete(special), label="Gebiet").classes("w-full")
            entwertung_field = ui.input("Entwertung").classes("w-full")
            besitz_field = ui.number("Besitz", min=0, precision=0).classes("w-full")
            variante_field = ui.input("Variante").classes("w-full")
            typ_field = ui.input("Typ").classes("w-full") if special else None

        def save_range():
            if lower_field.value is None or upper_field.value is None:
                ui.notify("Bitte Katalog-Unter- und Obergrenze eingeben.", type="negative")
                return
            if gebiet_field.value not in _configured_gebiete(special):
                ui.notify("Bitte ein Gebiet aus der Liste wählen.", type="negative")
                return

            lower = int(lower_field.value)
            upper = int(upper_field.value)
            if lower > upper:
                ui.notify("Die Untergrenze darf nicht größer als die Obergrenze sein.", type="negative")
                return
            if lower < 0 or upper > MAX_KATALOGNUMMER:
                ui.notify(
                    "Der Bereich muss zwischen 0 und 2147483647 liegen.",
                    type="negative",
                )
                return

            inserted = add_stamp_range(
                gebiet=gebiet_field.value,
                lower=lower,
                upper=upper,
                entwertung=(entwertung_field.value or "").strip(),
                besitz=int(besitz_field.value or 0),
                variante=(variante_field.value or "").strip(),
                special=special,
                typ=(typ_field.value or "").strip() if typ_field is not None else None,
            )
            skipped = upper - lower + 1 - inserted
            ui.notify(
                f"{inserted} Briefmarken hinzugefügt; {skipped} bereits vorhandene übersprungen.",
                type="positive",
            )

        with ui.row().classes("justify-end w-full"):
            ui.button("Bereich hinzufügen", icon="save", on_click=save_range)


@ui.page("/fehlliste")
def missing_stamps_page():
    build_missing_stamps_page(False)


@ui.page("/spezial/fehlliste")
def special_missing_stamps_page():
    build_missing_stamps_page(True)


def build_missing_stamps_page(special: bool):
    page_navigation(special)
    collection_title(special)

    with ui.card().classes("w-full"):
        ui.label("Fehlliste").classes("text-h6")
        gebiet_filter = ui.select(
            options=[ALL_GEBIETE] + get_gebiete(special=special),
            value=ALL_GEBIETE,
            label="Gebiet anzeigen",
            on_change=lambda: refresh(),
        ).classes("w-64")
        table = ui.table(
            columns=TABLE_COLUMNS[:-1] + ([TYP_COLUMN] if special else []),
            rows=get_missing_stamps(ALL_GEBIETE, special=special),
            row_key="id",
            pagination=100,
        ).classes("w-full")

    def refresh():
        table.rows = get_missing_stamps(gebiet_filter.value or ALL_GEBIETE, special=special)
        table.pagination = {**table.pagination, "page": 1}
        table.update()


@ui.page("/abfrage")
def query_page():
    build_query_page(False)


@ui.page("/spezial/abfrage")
def special_query_page():
    build_query_page(True)


def build_query_page(special: bool):
    page_navigation(special)
    collection_title(special)

    with ui.card().classes("w-full"):
        ui.label("Sammlung").classes("text-h6")
        gebiet_filter = ui.select(
            options=[ALL_GEBIETE] + get_gebiete(special=special),
            value=ALL_GEBIETE,
            label="Gebiet anzeigen",
            on_change=lambda: refresh(),
        ).classes("w-64")
        katalognummer_filter = ui.input(
            "Katalognummer suchen",
            on_change=lambda: refresh(),
        ).props("clearable").classes("w-64")
        with ui.row().classes("w-full no-wrap items-start"):
            table = ui.table(
                columns=TABLE_COLUMNS[:-1] + ([TYP_COLUMN] if special else []) + TABLE_COLUMNS[-1:],
                rows=get_stamps_by_gebiet(ALL_GEBIETE, special=special),
                row_key="id",
                pagination=100,
            ).classes("flex-grow")
            with ui.card().classes("w-[28rem]"):
                ui.label("Besitz nach Entwertung").classes("text-subtitle1")
                stats_table = ui.table(
                    columns=STAT_COLUMNS, rows=get_collection_stats(ALL_GEBIETE, special=special), row_key="entwertung"
                ).classes("w-full")
                stats_total = ui.label().classes("text-bold")
        table.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
                <q-btn flat dense icon="edit" @click="() => $parent.$emit('edit', props.row)" />
                <q-btn flat dense icon="delete" color="negative"
                       @click="() => $parent.$emit('delete', props.row)" />
            </q-td>
            """,
        )

    def refresh():
        gebiet_filter.options = [ALL_GEBIETE] + get_gebiete(special=special)
        gebiet_filter.update()
        selected = gebiet_filter.value or ALL_GEBIETE
        table.rows = get_stamps_by_gebiet(selected, katalognummer_filter.value or "", special=special)
        table.pagination = {**table.pagination, "page": 1}
        table.update()
        stats_table.rows = get_collection_stats(selected, special=special)
        stats_table.update()
        owned = sum(row["besitz"] for row in stats_table.rows)
        total = sum(row["gesamt"] for row in stats_table.rows)
        share = round(owned / total * 100, 1) if total else 0
        stats_total.text = f"Im Besitz: {owned} von {total} ({share} %)"

    with ui.dialog() as dialog, ui.card().classes("w-[600px]"):
        ui.label("Briefmarke bearbeiten").classes("text-h6")
        edit_form = stamp_form(special)
        edit_state: dict[str, Optional[int]] = {"id": None}
        existing_gebiet: Optional[str] = None

        def save_edit():
            data = read_form(edit_form, special=special, existing_gebiet=existing_gebiet)
            if data is None or edit_state["id"] is None:
                return
            if not update_stamp(edit_state["id"], **data, special=special):
                ui.notify("Briefmarke nicht gefunden.", type="negative")
                return
            ui.notify("Änderungen gespeichert.", type="positive")
            dialog.close()
            refresh()

        with ui.row().classes("justify-end w-full"):
            ui.button("Abbrechen", on_click=dialog.close).props("flat")
            ui.button("Speichern", icon="save", on_click=save_edit)

    def open_edit(row: dict):
        nonlocal existing_gebiet
        edit_state["id"] = row["id"]
        existing_gebiet = row.get("gebiet") if special else None
        for name, inp in edit_form.items():
            if isinstance(inp, ui.number):
                inp.value = row.get(name)
            elif isinstance(inp, ui.select):
                options = list(_configured_gebiete(special))
                if existing_gebiet and existing_gebiet not in options:
                    options.append(existing_gebiet)
                inp.set_options(options, value=row.get(name) if row.get(name) in options else None)
            else:
                inp.value = row.get(name) or ""
        dialog.open()

    def remove(row: dict):
        if delete_stamp(row["id"], special=special):
            ui.notify(f"Briefmarke {row['id']} gelöscht.", type="info")
        refresh()

    table.on("edit", lambda e: open_edit(e.args))
    table.on("delete", lambda e: remove(e.args))
    refresh()


if __name__ in {"__main__", "__mp_main__"}:
    initialize_stamps_table()
    initialize_special_stamps_table()
    # reload=False: a reloader subprocess would try to open the same DuckDB file a second time.
    ui.run(title="Briefmarkensammlung", port=8080, reload=False)
