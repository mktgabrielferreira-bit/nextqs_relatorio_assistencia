from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
import math
import re
from typing import BinaryIO

import gspread
import pandas as pd


TIFLUX_TICKET_SHEET = "tiflux-ticket"
TIFLUX_CHAT_SHEET = "tiflux-chat"

TICKET_COLUMNS = [
    "ticket_id",
    "cliente",
    "responsavel",
    "status",
    "estagio",
    "origem",
    "catalogo",
    "area",
    "item",
    "natureza",
    "causa",
    "aberto_em",
    "primeiro_atendimento_em",
    "vencimento_atendimento_em",
    "fechado_em",
    "vencimento_solucao_em",
    "sla_atendimento",
    "sla_solucao",
    "prazo_atendimento_atual_min",
    "prazo_solucao_atual_min",
    "duracao_util_min",
    "tempo_apontado_min",
    "tempo_pendente_min",
    "vencimento_estagio_em",
    "prazo_estagio_atual_min",
    "reaberturas",
    "avaliacao_nota",
    "avaliacao_comentario",
    "descricao_resumida",
    "unidade",
    "meta_sla_percentual",
    "referencia_em",
]

CHAT_COLUMNS = [
    "chat_id",
    "ticket_id",
    "cliente",
    "responsavel",
    "departamento",
    "origem",
    "status",
    "iniciado_em",
    "assumido_em",
    "primeira_mensagem_em",
    "finalizado_em",
    "espera_util_min",
    "duracao_util_min",
    "catalogo",
    "area",
    "item",
    "categoria_fonte",
    "avaliacao_nota",
    "avaliacao_comentario",
    "solicitante",
    "telefone_id",
    "referencia_em",
]

TICKET_DATE_COLUMNS = [
    "aberto_em",
    "primeiro_atendimento_em",
    "vencimento_atendimento_em",
    "fechado_em",
    "vencimento_solucao_em",
    "vencimento_estagio_em",
    "referencia_em",
]
CHAT_DATE_COLUMNS = [
    "iniciado_em",
    "assumido_em",
    "primeira_mensagem_em",
    "finalizado_em",
    "referencia_em",
]

TICKET_NUMERIC_COLUMNS = [
    "prazo_atendimento_atual_min",
    "prazo_solucao_atual_min",
    "duracao_util_min",
    "tempo_apontado_min",
    "tempo_pendente_min",
    "reaberturas",
    "avaliacao_nota",
    "meta_sla_percentual",
]
CHAT_NUMERIC_COLUMNS = ["espera_util_min", "duracao_util_min", "avaliacao_nota"]


@dataclass(frozen=True)
class TifluxImportResult:
    tickets: pd.DataFrame
    chats: pd.DataFrame
    warnings: tuple[str, ...]


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _normalize_nulls_and_text(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result.columns = [str(column).strip() for column in result.columns]

    def clean(value: object) -> object:
        if _is_missing(value):
            return pd.NA
        if isinstance(value, str):
            value = value.strip()
            if value == "" or value.upper() == "NA":
                return pd.NA
        return value

    return result.map(clean)


def _canonical_identifier(value: object, *, allow_missing: bool, field: str) -> object:
    if _is_missing(value):
        if allow_missing:
            return pd.NA
        raise ValueError(f"{field} não pode ficar vazio.")
    if isinstance(value, bool):
        raise ValueError(f"{field} deve ser um número inteiro.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        raise ValueError(f"{field} deve ser um número inteiro.")

    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return str(int(text))
    if re.fullmatch(r"\d+\.0+", text):
        return str(int(float(text)))
    raise ValueError(f"{field} contém um identificador inválido: {text!r}.")


def _normalize_identifier_column(
    df: pd.DataFrame,
    field: str,
    *,
    allow_missing: bool,
    errors: list[str],
) -> None:
    normalized = []
    for row_number, value in enumerate(df[field], start=2):
        try:
            normalized.append(
                _canonical_identifier(value, allow_missing=allow_missing, field=field)
            )
        except ValueError as exc:
            errors.append(f"Linha {row_number}: {exc}")
            normalized.append(pd.NA)
    df[field] = pd.Series(normalized, index=df.index, dtype="object")


def _normalize_phone(value: object) -> object:
    if _is_missing(value):
        return pd.NA
    if isinstance(value, bool):
        raise ValueError("telefone_id deve conter somente dígitos.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        raise ValueError("telefone_id deve conter somente dígitos.")
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return text
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    raise ValueError(f"telefone_id inválido: {text!r}.")


def _validate_headers(df: pd.DataFrame, required: list[str], sheet_name: str) -> list[str]:
    errors: list[str] = []
    duplicates = pd.Index(df.columns)[pd.Index(df.columns).duplicated()].unique().tolist()
    if duplicates:
        errors.append(
            f"A aba {sheet_name} possui cabeçalhos duplicados: {', '.join(map(str, duplicates))}."
        )
    missing = [column for column in required if column not in df.columns]
    if missing:
        errors.append(
            f"A aba {sheet_name} não possui os campos obrigatórios: {', '.join(missing)}."
        )
    return errors


def _validate_dates(
    df: pd.DataFrame,
    columns: list[str],
    sheet_name: str,
    errors: list[str],
) -> None:
    for column in columns:
        source = df[column]
        parsed = pd.to_datetime(source, errors="coerce")
        invalid = source.notna() & parsed.isna()
        if invalid.any():
            rows = ", ".join(str(index + 2) for index in df.index[invalid][:8])
            errors.append(
                f"A aba {sheet_name} possui datas inválidas em {column} (linhas {rows})."
            )
        df[column] = parsed


def _validate_numeric(
    df: pd.DataFrame,
    columns: list[str],
    sheet_name: str,
    errors: list[str],
) -> None:
    for column in columns:
        source = df[column]
        parsed = pd.to_numeric(source, errors="coerce")
        invalid = source.notna() & parsed.isna()
        finite = parsed.dropna().map(math.isfinite)
        if invalid.any() or not finite.all():
            bad_rows = invalid.copy()
            if not finite.all():
                bad_rows.loc[finite.index[~finite]] = True
            rows = ", ".join(str(index + 2) for index in df.index[bad_rows][:8])
            errors.append(
                f"A aba {sheet_name} possui números inválidos em {column} (linhas {rows})."
            )
        df[column] = parsed


def _normalize_code(value: object) -> object:
    if _is_missing(value):
        return pd.NA
    text = str(value).strip().upper().replace(" ", "_")
    return text.replace("NÃO", "NAO")


def _validate_allowed(
    df: pd.DataFrame,
    column: str,
    allowed: set[str],
    sheet_name: str,
    errors: list[str],
) -> None:
    df[column] = df[column].map(_normalize_code)
    invalid_values = sorted(
        set(df.loc[df[column].notna(), column].astype(str)) - allowed
    )
    if invalid_values:
        errors.append(
            f"A aba {sheet_name} possui valores inválidos em {column}: "
            f"{', '.join(invalid_values)}."
        )


def _validate_duplicates(
    df: pd.DataFrame,
    id_column: str,
    sheet_name: str,
    errors: list[str],
) -> None:
    duplicate_ids = df.loc[df[id_column].duplicated(keep=False), id_column].dropna().unique()
    if len(duplicate_ids) == 0:
        return

    conflicting: list[str] = []
    repeated: list[str] = []
    for identifier in duplicate_ids:
        group = df[df[id_column] == identifier]
        if len(group.drop_duplicates()) > 1:
            conflicting.append(str(identifier))
        else:
            repeated.append(str(identifier))
    if conflicting:
        errors.append(
            f"A aba {sheet_name} possui IDs duplicados com dados conflitantes em "
            f"{id_column}: {', '.join(conflicting[:12])}."
        )
    if repeated:
        errors.append(
            f"A aba {sheet_name} possui IDs repetidos em {id_column}: "
            f"{', '.join(repeated[:12])}."
        )


def _validate_ranges(tickets: pd.DataFrame, chats: pd.DataFrame, errors: list[str]) -> None:
    for sheet_name, frame in (("Tickets", tickets), ("Chats", chats)):
        if (frame["avaliacao_nota"].dropna().between(1, 5) == False).any():  # noqa: E712
            errors.append(f"A aba {sheet_name} possui avaliacao_nota fora do intervalo de 1 a 5.")

    if (tickets["meta_sla_percentual"].dropna().between(0, 100) == False).any():  # noqa: E712
        errors.append("A aba Tickets possui meta_sla_percentual fora do intervalo de 0 a 100.")
    if (tickets["reaberturas"].dropna() < 0).any():
        errors.append("A aba Tickets possui reaberturas negativas.")
    if not tickets["reaberturas"].dropna().map(lambda value: float(value).is_integer()).all():
        errors.append("A aba Tickets possui reaberturas não inteiras.")

    minute_columns = {
        "Tickets": [column for column in TICKET_NUMERIC_COLUMNS if column not in {"avaliacao_nota", "meta_sla_percentual", "reaberturas"}],
        "Chats": [column for column in CHAT_NUMERIC_COLUMNS if column != "avaliacao_nota"],
    }
    for sheet_name, frame in (("Tickets", tickets), ("Chats", chats)):
        for column in minute_columns[sheet_name]:
            if (frame[column].dropna() < 0).any():
                errors.append(f"A aba {sheet_name} possui minutos negativos em {column}.")


def load_and_validate_tiflux_xlsx(source: bytes | BinaryIO) -> TifluxImportResult:
    if isinstance(source, bytes):
        source = BytesIO(source)
    if hasattr(source, "seek"):
        source.seek(0)

    try:
        workbook = pd.ExcelFile(source, engine="openpyxl")
    except Exception as exc:
        raise ValueError(f"Não foi possível abrir o arquivo XLSX: {exc}") from exc

    required_sheets = {"Tickets", "Chats"}
    missing_sheets = sorted(required_sheets - set(workbook.sheet_names))
    if missing_sheets:
        raise ValueError(
            "O arquivo não possui as abas obrigatórias: " + ", ".join(missing_sheets) + "."
        )

    tickets = _normalize_nulls_and_text(
        pd.read_excel(workbook, sheet_name="Tickets", keep_default_na=False)
    )
    chats = _normalize_nulls_and_text(
        pd.read_excel(workbook, sheet_name="Chats", keep_default_na=False)
    )

    errors = _validate_headers(tickets, TICKET_COLUMNS, "Tickets")
    errors.extend(_validate_headers(chats, CHAT_COLUMNS, "Chats"))
    if errors:
        raise ValueError("\n".join(errors))

    _normalize_identifier_column(tickets, "ticket_id", allow_missing=False, errors=errors)
    _normalize_identifier_column(chats, "chat_id", allow_missing=False, errors=errors)
    _normalize_identifier_column(chats, "ticket_id", allow_missing=True, errors=errors)

    phones = []
    for row_number, value in enumerate(chats["telefone_id"], start=2):
        try:
            phones.append(_normalize_phone(value))
        except ValueError as exc:
            errors.append(f"Linha {row_number}: {exc}")
            phones.append(pd.NA)
    chats["telefone_id"] = pd.Series(phones, index=chats.index, dtype="object")

    _validate_dates(tickets, TICKET_DATE_COLUMNS, "Tickets", errors)
    _validate_dates(chats, CHAT_DATE_COLUMNS, "Chats", errors)
    _validate_numeric(tickets, TICKET_NUMERIC_COLUMNS, "Tickets", errors)
    _validate_numeric(chats, CHAT_NUMERIC_COLUMNS, "Chats", errors)

    stage_source = tickets["prazo_estagio_atual_min"]
    stage_not_applicable = stage_source.astype("string").str.upper().eq("NAO_APLICAVEL")
    stage_numeric = pd.to_numeric(stage_source.mask(stage_not_applicable), errors="coerce")
    stage_invalid = stage_source.notna() & ~stage_not_applicable & stage_numeric.isna()
    if stage_invalid.any():
        rows = ", ".join(str(index + 2) for index in tickets.index[stage_invalid][:8])
        errors.append(
            "A aba Tickets possui números inválidos em prazo_estagio_atual_min "
            f"(linhas {rows})."
        )

    _validate_allowed(tickets, "status", {"ABERTO", "FECHADO"}, "Tickets", errors)
    _validate_allowed(chats, "status", {"FECHADO", "CANCELADO"}, "Chats", errors)
    for column in ("sla_atendimento", "sla_solucao"):
        _validate_allowed(
            tickets,
            column,
            {"SIM", "NAO", "EM_ANDAMENTO"},
            "Tickets",
            errors,
        )

    _validate_duplicates(tickets, "ticket_id", "Tickets", errors)
    _validate_duplicates(chats, "chat_id", "Chats", errors)
    _validate_ranges(tickets, chats, errors)

    if errors:
        raise ValueError("\n".join(errors))

    warnings: list[str] = []
    ticket_ids = set(tickets["ticket_id"].dropna().astype(str))
    linked_ids = set(chats["ticket_id"].dropna().astype(str))
    missing_links = sorted(linked_ids - ticket_ids, key=lambda value: int(value))
    if missing_links:
        warnings.append(
            f"{len(missing_links)} ticket_id vinculado(s) em Chats não existe(m) na aba "
            f"Tickets: {', '.join(missing_links[:12])}."
        )

    return TifluxImportResult(tickets=tickets, chats=chats, warnings=tuple(warnings))


def _serialize_cell(value: object) -> object:
    if _is_missing(value):
        return ""
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            return int(value)
    return value


def _column_letter(index: int) -> str:
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _prepare_sheet_upsert(
    spreadsheet: object,
    sheet_name: str,
    frame: pd.DataFrame,
    id_column: str,
) -> tuple[object, list[list[object]], dict[str, int], bool]:
    created = False
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=sheet_name,
            rows=max(1000, len(frame) + 1),
            cols=max(26, len(frame.columns)),
        )
        created = True

    values = worksheet.get_all_values()
    existing_headers = [str(value).strip() for value in values[0]] if values else []
    while existing_headers and existing_headers[-1] == "":
        existing_headers.pop()
    if len(existing_headers) != len(set(existing_headers)):
        raise RuntimeError(f"A aba {sheet_name} possui cabeçalhos duplicados.")
    if existing_headers and id_column not in existing_headers:
        raise RuntimeError(
            f"A aba {sheet_name} já possui dados, mas não contém a chave {id_column}."
        )

    incoming_headers = [str(column).strip() for column in frame.columns]
    merged_headers = existing_headers + [
        column for column in incoming_headers if column not in existing_headers
    ]
    if not merged_headers:
        merged_headers = incoming_headers

    records: list[dict[str, object]] = []
    positions: dict[str, int] = {}
    for sheet_row, row in enumerate(values[1:], start=2):
        normalized = row[: len(existing_headers)] + [""] * max(
            0, len(existing_headers) - len(row)
        )
        if not any(str(value).strip() for value in normalized):
            continue
        record = dict(zip(existing_headers, normalized))
        raw_identifier = record.get(id_column, "")
        try:
            identifier = _canonical_identifier(
                raw_identifier,
                allow_missing=False,
                field=id_column,
            )
        except ValueError as exc:
            raise RuntimeError(f"Aba {sheet_name}, linha {sheet_row}: {exc}") from exc
        identifier = str(identifier)
        if identifier in positions:
            raise RuntimeError(
                f"A aba {sheet_name} já contém o {id_column} duplicado: {identifier}."
            )
        positions[identifier] = len(records)
        records.append({header: record.get(header, "") for header in merged_headers})

    inserted = 0
    updated = 0
    unchanged = 0
    for _, row in frame.iterrows():
        incoming = {header: _serialize_cell(row[header]) for header in incoming_headers}
        identifier = str(incoming[id_column])
        if identifier in positions:
            position = positions[identifier]
            current = records[position]
            changed = any(str(current.get(header, "")) != str(incoming[header]) for header in incoming_headers)
            if changed:
                current.update(incoming)
                updated += 1
            else:
                unchanged += 1
        else:
            record = {header: "" for header in merged_headers}
            record.update(incoming)
            positions[identifier] = len(records)
            records.append(record)
            inserted += 1

    matrix: list[list[object]] = [merged_headers]
    matrix.extend([[record.get(header, "") for header in merged_headers] for record in records])
    headers_changed = merged_headers != existing_headers
    should_write = created or headers_changed or inserted > 0 or updated > 0
    counts = {"inseridos": inserted, "atualizados": updated, "inalterados": unchanged}
    return worksheet, matrix, counts, should_write


def upsert_tiflux_frames(
    gspread_client: object,
    spreadsheet_id: str,
    tickets: pd.DataFrame,
    chats: pd.DataFrame,
) -> dict[str, dict[str, int]]:
    spreadsheet = gspread_client.open_by_key(spreadsheet_id)
    prepared = [
        (TIFLUX_TICKET_SHEET, tickets, "ticket_id"),
        (TIFLUX_CHAT_SHEET, chats, "chat_id"),
    ]

    updates = []
    result: dict[str, dict[str, int]] = {}
    for sheet_name, frame, id_column in prepared:
        worksheet, matrix, counts, should_write = _prepare_sheet_upsert(
            spreadsheet,
            sheet_name,
            frame,
            id_column,
        )
        result[sheet_name] = counts
        if not should_write:
            continue
        row_count = len(matrix)
        column_count = len(matrix[0])
        if worksheet.row_count < row_count or worksheet.col_count < column_count:
            worksheet.resize(
                rows=max(worksheet.row_count, row_count),
                cols=max(worksheet.col_count, column_count),
            )
        end_cell = f"{_column_letter(column_count)}{row_count}"
        updates.append(
            {
                "range": f"'{sheet_name}'!A1:{end_cell}",
                "majorDimension": "ROWS",
                "values": matrix,
            }
        )

    if updates:
        spreadsheet.values_batch_update(
            {"valueInputOption": "RAW", "data": updates}
        )
    return result
