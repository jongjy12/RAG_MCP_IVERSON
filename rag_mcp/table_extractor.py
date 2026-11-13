"""Dynamic table detection and row chunking adapted from prototype-RAGProject/app.py.

This module contains parsing helpers only; it does not initialize Flask or models.
"""
from __future__ import annotations
import io
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
MAX_CHUNK_TOKENS = 180


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)):
            return bool(missing)
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def display_value(value: Any) -> str:
    """Render Excel/pandas scalar values consistently for prompts and the UI."""
    if is_blank(value):
        return ""
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text.lstrip(": ").strip() if text.startswith(":") else text


def normalize_name(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def unique_headers(headers: list[Any], start_col: int, warnings: list[str]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    formula_errors = (
        "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#N/A", "#NULL!", "#NUM!",
        "#SPILL!", "#CALC!",
    )
    for offset, raw_header in enumerate(headers):
        header = display_value(raw_header)
        column_name = get_column_letter(start_col + offset)
        if not header or header.startswith(formula_errors):
            if header.startswith(formula_errors):
                warnings.append(
                    f"Excel error {header} was used as a header in column {column_name}; "
                    f"the field is labeled 'Column {column_name} (formula error)'."
                )
                header = f"Column {column_name} (formula error)"
            else:
                header = f"Column {column_name}"
        base = header
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            header = f"{base}_{seen[base]}"
        result.append(header)
    return result


def qualify_table_label(sheet: str | None, label: Any) -> str:
    """Include the worksheet name in an extracted table's display label."""
    detail = str(label or "Table").strip()
    if not sheet:
        return detail

    sheet_name = str(sheet).strip()
    if normalize_name(detail) == normalize_name(sheet_name):
        return sheet_name
    if detail.casefold().startswith(f"{sheet_name} · ".casefold()):
        return detail
    if normalize_name(detail) in {"detected table", "detected region"}:
        detail = "Table"
    return f"{sheet_name} · {detail}"


def make_table(
    table_id: str,
    label: str,
    frame: pd.DataFrame,
    *,
    sheet: str | None = None,
    source_rows: list[int] | None = None,
    kind: str = "records",
    source_refs: list[str] | None = None,
    role: str | None = None,
    bounds: tuple[int, int, int, int] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    table_label = qualify_table_label(sheet, label)
    return {
        "id": table_id,
        "label": table_label,
        "frame": frame.reset_index(drop=True),
        "sheet": sheet,
        "source_rows": source_rows or list(range(1, len(frame) + 1)),
        "kind": kind,
        "source_refs": source_refs or [],
        "role": role or ("records" if kind == "records" else kind),
        "searchable": True,
        "bounds": bounds,
        "title": title,
    }


def dataframe_from_excel_range(
    worksheet,
    min_col: int,
    min_row: int,
    max_col: int,
    max_row: int,
    *,
    table_id: str,
    label: str,
    kind: str,
    warnings: list[str],
    role: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    raw_headers = [worksheet.cell(min_row, col).value for col in range(min_col, max_col + 1)]
    headers = unique_headers(raw_headers, min_col, warnings)
    records: list[dict[str, Any]] = []
    source_rows: list[int] = []

    for row_num in range(min_row + 1, max_row + 1):
        values = [worksheet.cell(row_num, col).value for col in range(min_col, max_col + 1)]
        if all(is_blank(value) for value in values):
            continue
        records.append(dict(zip(headers, values)))
        source_rows.append(row_num)

    return make_table(
        table_id,
        label,
        pd.DataFrame(records, columns=headers),
        sheet=worksheet.title,
        source_rows=source_rows,
        kind=kind,
        role=role,
        bounds=(min_row, max_row, min_col, max_col),
        title=title,
    )


INFO_PROFILE = (
    "Attributes that identify a project or describe its relatively stable background, "
    "such as its name, purpose, owner, scope, size, type, descriptive details, "
    "or modification information."
)


SUMMARY_PROFILE = (
    "A concise snapshot of project execution, such as current status, progress, "
    "resource allocation, totals, reporting dates, counts, or performance measures."
)


def worksheet_value_grid(worksheet) -> dict[int, list[Any]]:
    """Return only cells containing values; styled-but-empty cells do not define regions."""
    rows: dict[int, list[Any]] = {}
    stored_cells = getattr(worksheet, "_cells", None)
    if stored_cells is not None:
        cells = stored_cells.values()
    else:
        cells = (cell for row in worksheet.iter_rows() for cell in row)

    for cell in cells:
        if is_blank(cell.value):
            continue
        rows.setdefault(cell.row, []).append(cell)
    for row_cells in rows.values():
        row_cells.sort(key=lambda cell: cell.column)
    return rows


def cell_text_is_header(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= 120


def is_generic_header_row(cells: list[Any]) -> bool:
    """Recognize a header-like row by shape and uniqueness, not by required names."""
    if len(cells) < 2:
        return False
    values = [cell.value for cell in cells]
    if not all(cell_text_is_header(value) for value in values):
        return False
    normalized = [normalize_name(value) or str(value).strip().casefold() for value in values]
    unique_ratio = len(set(normalized)) / max(1, len(normalized))
    return unique_ratio >= 0.75


def is_record_header_candidate(cells: list[Any], minimum_density: float = 0.55) -> bool:
    """Reject sparse label/value rows that only resemble a header by text shape.

    Real record headers usually populate most columns in their span. Metadata
    layouts often place a few labels and values far apart, with blank columns
    between them; treating those gaps as columns creates false tables.
    """
    if not is_generic_header_row(cells):
        return False
    columns = {cell.column for cell in cells}
    if not columns:
        return False
    span = max(columns) - min(columns) + 1
    return len(columns) / span >= minimum_density


def looks_like_field_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > 100 or "\n" in text or text.startswith((":", "=")):
        return False
    return len(text.split()) <= 8 and not is_date_like(text)


def find_horizontal_pairs(cells: list[Any], minimum_pairs: int = 2) -> list[tuple[Any, Any, Any, Any]]:
    """Infer repeated label/value pairs from cell order without using column coordinates."""
    if len(cells) < minimum_pairs * 2:
        return []
    candidates = []
    for offset in (0, 1):
        pairs = []
        label_score = 0
        for position in range(offset, len(cells) - 1, 2):
            label_cell = cells[position]
            value_cell = cells[position + 1]
            if looks_like_field_label(label_cell.value) and not is_blank(value_cell.value):
                pairs.append((label_cell, value_cell))
                label_score += 1
        candidates.append((len(pairs), label_score, -offset, pairs))
    best = max(candidates, key=lambda item: item[:3])
    # At least two pairs indicate a horizontal key/value block rather than a title.
    return [
        (label.value, value.value, label, value)
        for label, value in best[3]
    ] if best[0] >= minimum_pairs else []


def is_date_like(value: Any) -> bool:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return True
    if is_blank(value):
        return False
    try:
        return not pd.isna(pd.to_datetime(value, errors="coerce", dayfirst=True))
    except (TypeError, ValueError):
        return False


def bounds_overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    first_min_row, first_max_row, first_min_col, first_max_col = first
    second_min_row, second_max_row, second_min_col, second_max_col = second
    return not (
        first_max_row < second_min_row
        or second_max_row < first_min_row
        or first_max_col < second_min_col
        or second_max_col < first_min_col
    )


def row_values_in_bounds(row_cells: list[Any], min_col: int, max_col: int) -> list[Any]:
    return [cell for cell in row_cells if min_col <= cell.column <= max_col]


def row_repeats_header(cells: list[Any], header_cells: list[Any], min_col: int, max_col: int) -> bool:
    expected = {
        cell.column: normalize_name(cell.value) or str(cell.value).strip().casefold()
        for cell in header_cells
    }
    actual = row_values_in_bounds(cells, min_col, max_col)
    matches = sum(
        (normalize_name(cell.value) or str(cell.value).strip().casefold()) == expected.get(cell.column)
        for cell in actual
        if cell.column in expected
    )
    return matches >= max(2, math.ceil(len(expected) * 0.6))


def header_has_aligned_records_below(
    row_grid: dict[int, list[Any]],
    row_num: int,
    header_cells: list[Any],
) -> bool:
    if not is_record_header_candidate(header_cells):
        return False
    min_col = min(cell.column for cell in header_cells)
    max_col = max(cell.column for cell in header_cells)
    header_columns = {cell.column for cell in header_cells}
    minimum_values = max(2, min(5, math.ceil(len(header_cells) * 0.08)))
    max_row = max(row_grid, default=row_num)
    for following_row in range(row_num + 1, min(max_row, row_num + 3) + 1):
        cells = row_values_in_bounds(row_grid.get(following_row, []), min_col, max_col)
        cell_columns = {cell.column for cell in cells}
        overlap = len(header_columns & cell_columns)
        alignment = overlap / max(1, min(len(header_columns), len(cell_columns)))
        if (
            len(cells) >= minimum_values
            and overlap >= minimum_values
            and alignment >= 0.6
            and not row_repeats_header(cells, header_cells, min_col, max_col)
        ):
            return True
    return False


def split_header_column_bands(cells: list[Any]) -> list[list[Any]]:
    """Split disjoint header runs when a fully blank column separates regions."""
    bands: list[list[Any]] = []
    current: list[Any] = []
    for cell in cells:
        if current and cell.column - current[-1].column > 1:
            bands.append(current)
            current = []
        current.append(cell)
    if current:
        bands.append(current)
    if len(bands) > 1 and all(len(band) >= 2 for band in bands):
        return bands
    return [cells]


def detect_record_region(
    row_grid: dict[int, list[Any]],
    header_row: int,
    key_value_rows: set[int],
    worksheet_title: str,
    selected_header_cells: list[Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Infer a row-based table using generic header/data alignment patterns."""
    header_cells = selected_header_cells or row_grid.get(header_row, [])
    if not is_record_header_candidate(header_cells):
        return None
    min_col = min(cell.column for cell in header_cells)
    max_col = max(cell.column for cell in header_cells)
    width = max_col - min_col + 1
    populated_header_count = len(header_cells)
    min_row_values = max(2, min(5, math.ceil(populated_header_count * 0.08)))
    max_value_row = max(row_grid, default=header_row)

    data_start = None
    # Permit a short blank or merged-header continuation below the header.
    for row_num in range(header_row + 1, min(max_value_row, header_row + 3) + 1):
        if row_num in key_value_rows:
            continue
        cells = row_values_in_bounds(row_grid.get(row_num, []), min_col, max_col)
        if len(cells) >= min_row_values and not row_repeats_header(cells, header_cells, min_col, max_col):
            data_start = row_num
            break
    if data_start is None:
        return None

    raw_headers = [
        next((cell.value for cell in header_cells if cell.column == col), None)
        for col in range(min_col, max_col + 1)
    ]
    headers = unique_headers(raw_headers, min_col, warnings if warnings is not None else [])
    records: list[dict[str, Any]] = []
    source_rows: list[int] = []
    blank_streak = 0
    last_row = data_start

    for row_num in range(data_start, max_value_row + 1):
        cells = row_values_in_bounds(row_grid.get(row_num, []), min_col, max_col)
        if not cells:
            blank_streak += 1
            if blank_streak >= 2 and records:
                break
            continue
        if records and row_repeats_header(cells, header_cells, min_col, max_col):
            # Repeated headers inside a continued table are not data rows.
            blank_streak = 0
            continue
        if row_num in key_value_rows:
            if records:
                break
            continue
        blank_streak = 0
        if len(cells) < min_row_values:
            continue
        values = [
            next((cell.value for cell in cells if cell.column == col), None)
            for col in range(min_col, max_col + 1)
        ]
        if sum(not is_blank(value) for value in values) < min_row_values:
            continue
        records.append(dict(zip(headers, values)))
        source_rows.append(row_num)
        last_row = row_num

    if not records:
        return None

    # A header that is also shaped like a key/value row is only accepted as a
    # record table when its data begins immediately or after one blank row.
    if header_row in key_value_rows and data_start - header_row > 2:
        return None

    title = nearby_title(row_grid, header_row)
    table = make_table(
        f"{normalize_name(worksheet_title).replace(' ', '_')}_table",
        title or "Detected Table",
        pd.DataFrame(records, columns=headers),
        sheet=worksheet_title,
        source_rows=source_rows,
        kind="records",
        role="records",
        bounds=(header_row, last_row, min_col, max_col),
        title=title,
    )
    return table


def nearby_title(row_grid: dict[int, list[Any]], table_start_row: int) -> str | None:
    for row_num in range(table_start_row - 1, max(0, table_start_row - 3), -1):
        cells = row_grid.get(row_num, [])
        if len(cells) == 1 and isinstance(cells[0].value, str):
            title = cells[0].value.strip()
            if title and len(title) <= 80 and not title.endswith((".", "!", "?")):
                return title
    return None


def detect_key_value_rows(
    row_grid: dict[int, list[Any]],
    excluded_bounds: list[tuple[int, int, int, int]],
    minimum_pairs: int = 2,
) -> dict[int, list[dict[str, Any]]]:
    pairs_by_row: dict[int, list[dict[str, Any]]] = {}
    for row_num, cells in row_grid.items():
        remaining = [
            cell for cell in cells
            if not any(
                bounds[0] <= row_num <= bounds[1] and bounds[2] <= cell.column <= bounds[3]
                for bounds in excluded_bounds
            )
        ]
        if header_has_aligned_records_below(row_grid, row_num, remaining):
            continue
        pairs = find_horizontal_pairs(remaining, minimum_pairs=minimum_pairs)
        if not pairs:
            continue
        row_pairs = [
            {
                "field": display_value(field),
                "value": display_value(value),
                "source_row": row_num,
                "source_ref": (
                    f"{row_grid[row_num][0].parent.title}!"
                    f"{get_column_letter(label_cell.column)}{row_num}/"
                    f"{get_column_letter(value_cell.column)}{row_num}"
                ),
                "label_cell": label_cell,
                "value_cell": value_cell,
                "section_title": nearby_title(row_grid, row_num),
            }
            for field, value, label_cell, value_cell in pairs
            if not is_blank(value)
        ]
        sibling_labels = ", ".join(pair["field"] for pair in row_pairs)
        for pair in row_pairs:
            pair["sibling_labels"] = sibling_labels
        pairs_by_row[row_num] = row_pairs
    return pairs_by_row


def group_key_value_rows(pairs_by_row: dict[int, list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_row = None
    for row_num in sorted(pairs_by_row):
        if current and previous_row is not None and row_num != previous_row + 1:
            groups.append(current)
            current = []
        current.extend(pairs_by_row[row_num])
        previous_row = row_num
    if current:
        groups.append(current)
    return groups


def vertical_key_value_pairs(table: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Recognize a two-column label/value region by its data shape, not header names."""
    frame = table["frame"]
    if table["kind"] != "records" or len(frame.columns) != 2 or len(frame) < 2:
        return None

    key_column = frame.iloc[:, 0]
    value_column = frame.iloc[:, 1]
    key_values = key_column.tolist()
    rendered_keys = [display_value(value) for value in key_values]
    valid_key_count = sum(
        isinstance(value, str)
        and bool(value.strip())
        and len(value.strip()) <= 100
        and len(value.strip().split()) <= 8
        and not value.strip().replace(".", "", 1).isdigit()
        and not is_date_like(value)
        for value in key_values
    )
    unique_ratio = len({normalize_name(value) for value in rendered_keys if value}) / max(1, len(rendered_keys))
    value_ratio = sum(not is_blank(value) for value in value_column.tolist()) / max(1, len(frame))
    if valid_key_count / len(frame) < 0.8 or unique_ratio < 0.7 or value_ratio < 0.7:
        return None

    min_row, _max_row, min_col, _max_col = table.get("bounds") or (1, len(frame) + 1, 1, 2)
    source_rows = table["source_rows"]
    return [
        {
            "field": rendered_keys[position],
            "value": display_value(value_column.iloc[position]),
            "source_row": source_rows[position] if position < len(source_rows) else min_row + position + 1,
            "source_ref": (
                f"{table['sheet']}!{get_column_letter(min_col)}{source_rows[position]}/"
                f"{get_column_letter(min_col + 1)}{source_rows[position]}"
            ) if table["sheet"] and position < len(source_rows) else None,
            "sibling_labels": rendered_keys[position],
            "section_title": table.get("title"),
        }
        for position in range(len(frame))
        if not is_blank(value_column.iloc[position]) and rendered_keys[position]
    ]


def remove_duplicate_key_value_groups(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Prefer the richer instance when an older, partial copy repeats the same fields."""
    ranked = sorted(
        groups,
        key=lambda group: (
            len({normalize_name(pair["field"]) for pair in group}),
            len(group),
            sum(is_date_like(pair["value"]) for pair in group),
        ),
        reverse=True,
    )
    selected: list[list[dict[str, Any]]] = []
    selected_labels: list[set[str]] = []
    for group in ranked:
        labels = {normalize_name(pair["field"]) for pair in group}
        duplicate = False
        for prior_labels in selected_labels:
            overlap = len(labels & prior_labels)
            denominator = min(len(labels), len(prior_labels)) or 1
            if overlap / denominator >= 0.7:
                duplicate = True
                break
        if not duplicate:
            selected.append(group)
            selected_labels.append(labels)
    return selected


def classify_key_value_pairs(pairs: list[dict[str, Any]], embedder) -> dict[str, list[dict[str, Any]]]:
    """Use semantic label similarity as a soft role signal, not a required schema."""
    if not pairs:
        return {"project_information": [], "project_summary": []}
    descriptions = [INFO_PROFILE, SUMMARY_PROFILE]
    labels = []
    for pair in pairs:
        value = pair["value"]
        if is_date_like(value):
            value_kind = "date-like value"
        else:
            value_kind = "numeric value" if pd.to_numeric(pd.Series([value]), errors="coerce").notna().iloc[0] else "text value"
        labels.append(
            f"Field label: {pair['field']}. Related labels in this row: "
            f"{pair.get('sibling_labels', '')}. Section title: "
            f"{pair.get('section_title') or 'not available'}. The value is a {value_kind}."
        )
    vectors = embedder.encode(labels + descriptions, convert_to_numpy=True, normalize_embeddings=True)
    label_vectors = np.asarray(vectors[: len(labels)], dtype=np.float32)
    profile_vectors = np.asarray(vectors[len(labels) :], dtype=np.float32)
    similarities = label_vectors @ profile_vectors.T

    classified = {"project_information": [], "project_summary": []}
    for pair, scores in zip(pairs, similarities):
        role = "project_information" if float(scores[0]) >= float(scores[1]) else "project_summary"
        classified[role].append(pair)
    return classified


def make_key_value_table(
    worksheet,
    table_id: str,
    label: str,
    role: str,
    pairs: list[dict[str, Any]],
) -> dict[str, Any]:
    records = [
        {
            "Field": pair["field"],
            "Value": pair["value"],
            "Source Row": pair["source_row"],
            "Source Cells": pair["source_ref"].split("!", 1)[-1],
        }
        for pair in pairs
    ]
    min_row = min((pair["source_row"] for pair in pairs), default=1)
    max_row = max((pair["source_row"] for pair in pairs), default=min_row)
    return make_table(
        table_id,
        label,
        pd.DataFrame(records, columns=["Field", "Value", "Source Row", "Source Cells"]),
        sheet=worksheet.title,
        source_rows=[pair["source_row"] for pair in pairs],
        kind="key_value",
        source_refs=[pair["source_ref"] for pair in pairs],
        role=role,
        bounds=(min_row, max_row, 1, worksheet.max_column),
    )


def key_value_table_label(worksheet, pairs: list[dict[str, Any]], fallback: str) -> str:
    section_titles = {
        str(pair.get("section_title") or "").strip()
        for pair in pairs
        if str(pair.get("section_title") or "").strip()
    }
    if len(section_titles) == 1:
        section_title = next(iter(section_titles))
        if len(section_title) <= 80 and not section_title.endswith((".", "!", "?")):
            return qualify_table_label(worksheet.title, section_title)
    return qualify_table_label(worksheet.title, fallback)


def detect_generic_record_tables(worksheet, row_grid, key_value_rows, excluded_bounds, warnings):
    candidates = []
    for header_row in sorted(row_grid):
        if any(bounds[0] <= header_row <= bounds[1] for bounds in excluded_bounds):
            continue
        header_cells = row_grid.get(header_row, [])
        if not is_record_header_candidate(header_cells):
            continue
        bands = split_header_column_bands(header_cells)
        band_tables = [
            table
            for band in bands
            if (table := detect_record_region(
                row_grid, header_row, key_value_rows, worksheet.title, band, warnings
            )) is not None
        ]
        # Separate parallel tables are discovered as their own bounding boxes.
        # If one band is not independently table-shaped, retain the combined region.
        possible_tables = band_tables if len(bands) > 1 and len(band_tables) == len(bands) else [
            detect_record_region(row_grid, header_row, key_value_rows, worksheet.title, warnings=warnings)
        ]
        for table in possible_tables:
            if table is None:
                continue
            if any(bounds_overlap(table["bounds"], bounds) for bounds in excluded_bounds):
                continue
            if any(bounds_overlap(table["bounds"], existing["bounds"]) for existing in candidates):
                continue
            table["id"] = (
                f"{normalize_name(worksheet.title).replace(' ', '_')}_table_{len(candidates) + 1}"
            )
            table["label"] = qualify_table_label(
                worksheet.title,
                table.get("title") or f"Table {len(candidates) + 1}",
            )
            candidates.append(table)
            excluded_bounds.append(table["bounds"])
    return candidates


def detect_fallback_regions(worksheet, row_grid, occupied_bounds, protected_rows, warnings):
    """Detect remaining header/data regions from value bands after stronger matches."""
    remaining: dict[int, list[Any]] = {}
    for row_num, cells in row_grid.items():
        if row_num in protected_rows:
            continue
        available = [
            cell for cell in cells
            if not any(
                bounds[0] <= row_num <= bounds[1] and bounds[2] <= cell.column <= bounds[3]
                for bounds in occupied_bounds
            )
        ]
        if available:
            remaining[row_num] = available

    row_bands: list[list[int]] = []
    current: list[int] = []
    for row_num in sorted(remaining):
        if current and row_num - current[-1] > 2:
            row_bands.append(current)
            current = []
        current.append(row_num)
    if current:
        row_bands.append(current)

    regions = []
    region_number = 0
    for rows in row_bands:
        columns = sorted({cell.column for row_num in rows for cell in remaining[row_num]})
        column_bands: list[list[int]] = []
        current_columns: list[int] = []
        for col in columns:
            if current_columns and col - current_columns[-1] > 1:
                column_bands.append(current_columns)
                current_columns = []
            current_columns.append(col)
        if current_columns:
            column_bands.append(current_columns)

        for column_band in column_bands:
            if len(column_band) < 2 or len(rows) < 2:
                continue
            header_row = next(
                (
                    row_num
                    for row_num in rows
                    if is_record_header_candidate(
                        row_values_in_bounds(remaining[row_num], column_band[0], column_band[-1])
                    )
                ),
                None,
            )
            if header_row is None:
                continue
            raw_headers = [
                next((cell.value for cell in remaining[header_row] if cell.column == col), None)
                for col in column_band
            ]
            headers = unique_headers(raw_headers, column_band[0], warnings)
            records, source_rows = [], []
            for row_num in rows:
                if row_num <= header_row:
                    continue
                values = [
                    next((cell.value for cell in remaining[row_num] if cell.column == col), None)
                    for col in column_band
                ]
                if sum(not is_blank(value) for value in values) >= 2:
                    records.append(dict(zip(headers, values)))
                    source_rows.append(row_num)
            if not records:
                continue
            region_number += 1
            bounds = (header_row, max(source_rows), column_band[0], column_band[-1])
            if any(bounds_overlap(bounds, used) for used in occupied_bounds):
                continue
            title = nearby_title(row_grid, header_row)
            regions.append(make_table(
                f"{normalize_name(worksheet.title).replace(' ', '_')}_region_{region_number}",
                title or f"Table {region_number}",
                pd.DataFrame(records, columns=headers),
                sheet=worksheet.title,
                source_rows=source_rows,
                kind="records",
                role="records",
                bounds=bounds,
                title=title,
            ))
            occupied_bounds.append(bounds)
    return regions


def detect_workbook_index_sheets(
    tables: list[dict[str, Any]],
    sheet_names: list[str],
) -> set[str]:
    """Find worksheet directories by checking for columns listing other sheet names."""
    normalized_sheet_names = {normalize_name(name) for name in sheet_names if normalize_name(name)}
    index_sheets: set[str] = set()

    for table in tables:
        if table.get("kind") != "records" or table["frame"].empty:
            continue
        current_sheet = normalize_name(table.get("sheet") or "")
        other_sheet_names = normalized_sheet_names - {current_sheet}
        if len(other_sheet_names) < 2:
            continue

        frame = table["frame"]
        for column in frame.columns:
            values = [
                normalize_name(display_value(value))
                for value in frame[column].tolist()
                if not is_blank(value)
            ]
            matches = [value for value in values if value in other_sheet_names]
            if values and len(set(matches)) >= 2 and len(matches) / len(values) >= 0.6:
                if table.get("sheet"):
                    index_sheets.add(table["sheet"])
                break

    return index_sheets


def parse_excel_tables(file_bytes: bytes, embedder) -> tuple[list[dict[str, Any]], list[str]]:
    workbook = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=False)
    tables: list[dict[str, Any]] = []
    warnings: list[str] = []
    for worksheet in workbook.worksheets:
        for cell in worksheet._cells.values():
            if isinstance(cell.value, (float, int)) and not isinstance(cell.value, bool) and '%' in cell.number_format:
                cell.value = cell.value * 100
    warnings.append("Excel percentage-formatted cells use the 0-100 percentage scale; blanks remain missing.")

    for worksheet in workbook.worksheets:
        row_grid = worksheet_value_grid(worksheet)
        sheet_tables: list[dict[str, Any]] = []
        excluded_bounds: list[tuple[int, int, int, int]] = []

        # Excel's named Table and AutoFilter references are useful boundary hints.
        for table_name in list(worksheet.tables.keys()):
            excel_table = worksheet.tables[table_name]
            min_col, min_row, max_col, max_row = range_boundaries(excel_table.ref)
            bounds = (min_row, max_row, min_col, max_col)
            sheet_tables.append(dataframe_from_excel_range(
                worksheet,
                min_col,
                min_row,
                max_col,
                max_row,
                table_id=f"{normalize_name(worksheet.title).replace(' ', '_')}_{normalize_name(table_name).replace(' ', '_')}",
                label=str(table_name),
                kind="records",
                warnings=warnings,
                role="records",
                title=nearby_title(row_grid, min_row),
            ))
            excluded_bounds.append(bounds)

        if worksheet.auto_filter.ref:
            try:
                min_col, min_row, max_col, max_row = range_boundaries(worksheet.auto_filter.ref)
                bounds = (min_row, max_row, min_col, max_col)
                if max_row > min_row and not any(bounds_overlap(bounds, used) for used in excluded_bounds):
                    sheet_tables.append(dataframe_from_excel_range(
                        worksheet,
                        min_col,
                        min_row,
                        max_col,
                        max_row,
                        table_id=f"{normalize_name(worksheet.title).replace(' ', '_')}_filtered_region",
                        label=nearby_title(row_grid, min_row) or "Table",
                        kind="records",
                        warnings=warnings,
                        role="records",
                        title=nearby_title(row_grid, min_row),
                    ))
                    excluded_bounds.append(bounds)
            except ValueError:
                warnings.append(f"Could not interpret the AutoFilter range on sheet '{worksheet.title}'.")

        # Detect ordinary row-based tables by header/data shape, with no required
        # column names. Key/value-like rows are used only to reject false headers.
        preliminary_pairs = detect_key_value_rows(row_grid, excluded_bounds)
        key_value_rows = set(preliminary_pairs)
        detected = detect_generic_record_tables(
            worksheet, row_grid, key_value_rows, excluded_bounds, warnings
        )
        sheet_tables.extend(detected)

        # Re-scan outside all row-table bounds. This also handles multiple
        # horizontal key/value sections and ignores merged/style-only blank cells.
        all_bounds = [table["bounds"] for table in sheet_tables if table.get("bounds")]
        pairs_by_row = detect_key_value_rows(row_grid, all_bounds, minimum_pairs=1)
        candidate_key_value_groups = group_key_value_rows(pairs_by_row)
        vertical_pairs = []
        converted_tables = set()
        converted_bounds = []
        for table in sheet_tables:
            pairs = vertical_key_value_pairs(table)
            if pairs:
                vertical_pairs.extend(pairs)
                converted_tables.add(id(table))
                if table.get("bounds"):
                    converted_bounds.append(table["bounds"])
        if vertical_pairs:
            candidate_key_value_groups.append(vertical_pairs)
            sheet_tables = [table for table in sheet_tables if id(table) not in converted_tables]
        key_value_groups = remove_duplicate_key_value_groups(candidate_key_value_groups)
        all_pairs = [pair for group in key_value_groups for pair in group]
        if all_pairs:
            classified = classify_key_value_pairs(all_pairs, embedder)
            sheet_key = normalize_name(worksheet.title).replace(" ", "_") or "sheet"
            if classified["project_information"]:
                sheet_tables.append(make_key_value_table(
                    worksheet,
                    f"{sheet_key}_project_information",
                    key_value_table_label(worksheet, classified["project_information"], "Project Info"),
                    "project_information",
                    classified["project_information"],
                ))
            if classified["project_summary"]:
                sheet_tables.append(make_key_value_table(
                    worksheet,
                    f"{sheet_key}_project_summary",
                    key_value_table_label(worksheet, classified["project_summary"], "Project Summary"),
                    "project_summary",
                    classified["project_summary"],
                ))

        record_bounds = [
            table["bounds"] for table in sheet_tables
            if table["kind"] == "records" and table.get("bounds")
        ]
        record_bounds.extend(converted_bounds)
        sheet_tables.extend(detect_fallback_regions(
            worksheet,
            row_grid,
            record_bounds,
            set(pairs_by_row),
            warnings,
        ))

        tables.extend(sheet_tables)

    # If no header-shaped region was found, split remaining content into row/column
    # bands separated by blank rows or columns rather than treating the whole sheet
    # as one table. This is a conservative fallback for unusual layouts.
    if not tables:
        for worksheet in workbook.worksheets:
            row_grid = worksheet_value_grid(worksheet)
            rows = sorted(row_grid)
            row_bands: list[list[int]] = []
            current = []
            for row_num in rows:
                if current and row_num - current[-1] > 2:
                    row_bands.append(current)
                    current = []
                current.append(row_num)
            if current:
                row_bands.append(current)

            region_number = 0
            for band in row_bands:
                columns = sorted({cell.column for row_num in band for cell in row_grid[row_num]})
                column_bands: list[list[int]] = []
                current_columns = []
                for col in columns:
                    if current_columns and col - current_columns[-1] > 2:
                        column_bands.append(current_columns)
                        current_columns = []
                    current_columns.append(col)
                if current_columns:
                    column_bands.append(current_columns)

                for column_band in column_bands:
                    if len(band) < 2 or len(column_band) < 2:
                        continue
                    region_number += 1
                    min_col, max_col = column_band[0], column_band[-1]
                    header_row = next(
                        (
                            row_num
                            for row_num in band
                            if is_record_header_candidate(
                                row_values_in_bounds(row_grid[row_num], min_col, max_col)
                            )
                            and header_has_aligned_records_below(
                                row_grid, row_num,
                                row_values_in_bounds(row_grid[row_num], min_col, max_col),
                            )
                        ),
                        None,
                    )
                    if header_row is None:
                        continue
                    headers = unique_headers(
                        [
                            next((cell.value for cell in row_grid[header_row] if cell.column == col), None)
                            for col in column_band
                        ],
                        column_band[0],
                        warnings,
                    )
                    records, source_rows = [], []
                    minimum_values = max(2, min(5, math.ceil(len(column_band) * 0.08)))
                    for row_num in band:
                        if row_num <= header_row:
                            continue
                        values = [
                            next((cell.value for cell in row_grid[row_num] if cell.column == col), None)
                            for col in column_band
                        ]
                        if sum(not is_blank(value) for value in values) >= minimum_values:
                            records.append(dict(zip(headers, values)))
                            source_rows.append(row_num)
                    if records:
                        tables.append(make_table(
                            f"{normalize_name(worksheet.title).replace(' ', '_')}_region_{region_number}",
                            f"Table {region_number}",
                            pd.DataFrame(records, columns=headers),
                            sheet=worksheet.title,
                            source_rows=source_rows,
                            kind="records",
                            role="records",
                            bounds=(header_row, max(source_rows), column_band[0], column_band[-1]),
                        ))

    if not tables:
        raise ValueError("No tabular regions were found in the workbook.")

    # Detect workbook directory sheets by their values, without relying on a
    # particular worksheet title or a fixed column header.
    index_sheets = detect_workbook_index_sheets(tables, workbook.sheetnames)
    for table in tables:
        if table.get("sheet") in index_sheets:
            table["role"] = "workbook_index"
            table["searchable"] = False
    for sheet_name in sorted(index_sheets):
        warnings.append(
            f"Worksheet '{sheet_name}' appears to list other workbook sheets and was excluded from question retrieval."
        )

    return tables, warnings


def parse_csv_table(file_bytes: bytes) -> list[dict[str, Any]]:
    frame = pd.read_csv(io.BytesIO(file_bytes))
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("The CSV file has no data rows.")
    frame.columns = [str(column).strip() or f"Column {i + 1}" for i, column in enumerate(frame.columns)]
    return [make_table(
        "data",
        "Data",
        frame,
        source_rows=list(range(2, len(frame) + 2)),
        kind="records",
        role="records",
    )]


def extract_tables(filename: str, file_bytes: bytes, embedder) -> tuple[list[dict[str, Any]], list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return parse_csv_table(file_bytes), []
    if suffix == ".xlsx":
        return parse_excel_tables(file_bytes, embedder)
    raise ValueError("Only CSV and XLSX files are supported.")


def token_count(text: str, tokenizer) -> int:
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            pass
    return max(1, math.ceil(len(text) / 4))


def split_value(value: str, token_budget: int, tokenizer) -> list[str]:
    if token_count(value, tokenizer) <= token_budget:
        return [value]
    if tokenizer is not None:
        try:
            token_ids = tokenizer.encode(value, add_special_tokens=False)
            pieces = [
                tokenizer.decode(token_ids[start : start + token_budget], skip_special_tokens=True)
                for start in range(0, len(token_ids), token_budget)
            ]
            return [piece for piece in pieces if piece]
        except Exception:
            pass
    char_budget = max(80, token_budget * 4)
    return [value[start : start + char_budget] for start in range(0, len(value), char_budget)]


def chunk_record(
    *,
    table: dict[str, Any],
    fields: list[tuple[str, Any]],
    source_row: int | None,
    source_ref: str | None,
    record_id: str | None,
    tokenizer,
    project_context: str | None = None,
) -> list[dict[str, Any]]:
    location = table["sheet"] or "uploaded file"
    prefix_parts = [f"Table: {table['label']}", f"Source: {location}"]
    if project_context and table.get("kind") == "records":
        prefix_parts.append(project_context)
    if source_row is not None and table["sheet"]:
        prefix_parts.append(f"Excel row: {source_row}")
    if record_id:
        prefix_parts.append(f"Record: {record_id}")
    prefix = " | ".join(prefix_parts)
    prefix_tokens = token_count(prefix, tokenizer)

    output: list[dict[str, Any]] = []
    current_lines = [prefix]
    current_tokens = prefix_tokens

    def flush():
        nonlocal current_lines, current_tokens
        if len(current_lines) > 1:
            output.append({
                "table_id": table["id"],
                "table_label": table["label"],
                "source_sheet": table["sheet"],
                "source_row": source_row,
                "source_ref": source_ref,
                "record_id": record_id,
                "text": "\n".join(current_lines),
            })
        current_lines = [prefix]
        current_tokens = prefix_tokens

    for field, raw_value in fields:
        if is_blank(raw_value):
            continue
        value = display_value(raw_value)
        label = str(field).strip()
        per_value_budget = max(12, MAX_CHUNK_TOKENS - prefix_tokens - token_count(label + ": ", tokenizer) - 8)
        pieces = split_value(value, per_value_budget, tokenizer)
        for piece_num, piece in enumerate(pieces, start=1):
            continuation = f" (part {piece_num})" if len(pieces) > 1 else ""
            line = f"{label}{continuation}: {piece}"
            line_tokens = token_count(line, tokenizer)
            if len(current_lines) > 1 and current_tokens + line_tokens > MAX_CHUNK_TOKENS:
                flush()
            current_lines.append(line)
            current_tokens += line_tokens
    flush()
    return output


def build_chunks(
    tables: list[dict[str, Any]],
    tokenizer,
    project_context: str | None = None,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for table in tables:
        if not table.get("searchable", True):
            continue
        frame = table["frame"]
        if table["kind"] == "key_value":
            for position, (_, row) in enumerate(frame.iterrows()):
                source_row = int(row["Source Row"])
                source_ref = table["source_refs"][position] if position < len(table["source_refs"]) else None
                field_name = display_value(row.get("Field"))
                chunks.extend(chunk_record(
                    table=table,
                    fields=[(field_name, row.get("Value"))],
                    source_row=source_row,
                    source_ref=source_ref,
                    record_id=field_name,
                    tokenizer=tokenizer,
                ))
            continue

        for position, (_, row) in enumerate(frame.iterrows()):
            source_row = table["source_rows"][position] if position < len(table["source_rows"]) else None
            fields = [(str(column), row[column]) for column in frame.columns]
            record_id = display_value(row.iloc[0]) if len(row) and not is_blank(row.iloc[0]) else str(position + 1)
            source_ref = f"{table['sheet']}!row {source_row}" if table["sheet"] and source_row else None
            chunks.extend(chunk_record(
                table=table,
                fields=fields,
                source_row=source_row,
                source_ref=source_ref,
                record_id=record_id,
                tokenizer=tokenizer,
                project_context=project_context,
            ))

    return chunks
