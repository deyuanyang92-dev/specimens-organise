"""Classification Excel/UI columns and taxonomy-match field mapping.

Column order is the Excel write order. Keep changes backward-compatible with
existing workbooks; missing new columns are appended when a workspace opens.
"""

from __future__ import annotations

from typing import Any


CLASSIFICATION_COLUMNS = [
    "入库编号*",
    "种名*",
    "种拉丁",
    "属名",
    "科*",
    "科拉丁",
    "目",
    "纲",
    "门",
    "备注",
]

REQUIRED_CLASSIFICATION_COLUMNS = [
    "入库编号*",
    "种名*",
    "科*",
]

EDITABLE_CLASSIFICATION_COLUMNS = [
    field for field in CLASSIFICATION_COLUMNS if field != "入库编号*"
]

# Classification columns where typing should search the taxonomy preset table.
SPECIES_LOOKUP_INPUT_COLUMNS = {"种名*", "种拉丁", "属名"}
FAMILY_LOOKUP_INPUT_COLUMNS = {"科*", "科拉丁"}
TAXONOMY_LOOKUP_INPUT_COLUMNS = SPECIES_LOOKUP_INPUT_COLUMNS | FAMILY_LOOKUP_INPUT_COLUMNS

# Species match attribute -> classification column.
SPECIES_MATCH_TO_CLASSIFICATION_COLUMNS = {
    "种名*": "chinese_name",
    "种拉丁": "latin_name",
    "属名": "genus_name",
    "科*": "family_name",
    "科拉丁": "family_latin",
}

# Family match attribute -> classification column.
FAMILY_MATCH_TO_CLASSIFICATION_COLUMNS = {
    "科*": "family_name",
    "科拉丁": "family_latin",
}

# Classification fields included in import conflict summaries.
CLASSIFICATION_SUMMARY_FIELDS = [
    ("种名", "种名*"),
    ("科", "科*"),
]


def classification_values_from_species_match(match: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for column, attr in SPECIES_MATCH_TO_CLASSIFICATION_COLUMNS.items():
        if column not in CLASSIFICATION_COLUMNS:
            continue
        values[column] = str(getattr(match, attr, "") or "").strip()
    return values


def classification_values_from_family_match(match: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for column, attr in FAMILY_MATCH_TO_CLASSIFICATION_COLUMNS.items():
        if column not in CLASSIFICATION_COLUMNS:
            continue
        values[column] = str(getattr(match, attr, "") or "").strip()
    return values
