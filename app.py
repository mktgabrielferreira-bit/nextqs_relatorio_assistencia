import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Optional

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
from google.oauth2.service_account import Credentials

from tiflux import (
    CHAT_DATE_COLUMNS,
    CHAT_NUMERIC_COLUMNS,
    TICKET_DATE_COLUMNS,
    TICKET_NUMERIC_COLUMNS,
    TIFLUX_CHAT_SHEET,
    TIFLUX_TICKET_SHEET,
    load_and_validate_tiflux_xlsx,
    upsert_tiflux_frames,
)


# =============================
# Paleta / Config
# =============================
COR1 = "#1896D8"
COR2 = "#CC1B63"
COR3 = "#342B38"
BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "assets" / "nextqs_logo.png"

st.set_page_config(page_title="NextQS Dashboard", layout="wide")


# =============================
# Segurança
# =============================
def require_password() -> None:
    app_pwd = st.secrets.get("app_password") or st.secrets.get("SENHA_DASH")
    if not app_pwd:
        return

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    st.markdown(
        """
        <div style="text-align:center; padding: 56px 0 8px 0;">
            <h1 style="font-size: 44px; margin-bottom: 8px;">🔒 Acesso restrito</h1>
            <p style="opacity:0.75; font-size: 18px; margin: 0;">
                Digite a senha para acessar o dashboard
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _, mid, _ = st.columns([2.2, 2.6, 2.2])
    with mid:
        senha = st.text_input("Senha de acesso", type="password")
        if st.button("Entrar", use_container_width=True):
            if senha == app_pwd:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Senha incorreta.")
    st.stop()


require_password()


# =============================
# Sheets
# =============================
SCOPES_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SCOPES_READWRITE = ["https://www.googleapis.com/auth/spreadsheets"]


class DuplicateRowError(RuntimeError):
    pass


def _get_gspread_client(scopes: list[str]):
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"],
        scopes=scopes,
    )
    return gspread.authorize(creds)


def read_sheet(
    spreadsheet_id: str,
    sheet_name: Optional[str],
    *,
    fallback_to_first: bool = True,
) -> pd.DataFrame:
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"],
        scopes=SCOPES_READONLY,
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
    except gspread.WorksheetNotFound:
        if not fallback_to_first:
            return pd.DataFrame()
        ws = sh.sheet1

    values = ws.get_all_values()
    if not values or len(values) < 2:
        return pd.DataFrame()

    headers = [h.strip() for h in values[0]]
    while headers and headers[-1] == "":
        headers.pop()

    rows = values[1:]
    norm_rows = []
    n = len(headers)
    for r in rows:
        r = r[:n] + [""] * max(0, n - len(r))
        if all(str(x).strip() == "" for x in r):
            continue
        norm_rows.append(r)

    return pd.DataFrame(norm_rows, columns=headers)


def append_row_to_sheet(
    spreadsheet_id: str,
    sheet_name: Optional[str],
    values_by_header: dict[str, object],
) -> None:
    gc = _get_gspread_client(SCOPES_READWRITE)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
    except Exception:
        ws = sh.sheet1

    headers = [h.strip() for h in ws.row_values(1)]
    if not headers:
        raise RuntimeError("A aba não possui cabeçalho na primeira linha.")

    row = []
    for h in headers:
        row.append("" if values_by_header.get(h.strip()) is None else str(values_by_header.get(h.strip(), "")))

    def _normalize_row(values: list[object]) -> list[str]:
        return [str(v).strip() for v in values[: len(headers)] + [""] * max(0, len(headers) - len(values))]

    new_row_norm = _normalize_row(row)
    existing_rows = ws.get_all_values()[1:]
    for existing in existing_rows:
        existing_norm = _normalize_row(existing)
        if any(existing_norm) and existing_norm == new_row_norm:
            raise DuplicateRowError("Registro duplicado: uma linha igual ja existe na planilha.")

    col_a = ws.get(f"A2:A{ws.row_count}")
    if len(col_a) < (ws.row_count - 1):
        col_a = col_a + [[]] * ((ws.row_count - 1) - len(col_a))

    first_empty_row = None
    for offset, cell in enumerate(col_a, start=2):
        val = str(cell[0]).strip() if cell and len(cell) > 0 else ""
        if val == "":
            first_empty_row = offset
            break

    if first_empty_row is None:
        ws.append_row(row, value_input_option="USER_ENTERED")
    else:
        ws.insert_row(row, index=first_empty_row, value_input_option="USER_ENTERED")


# =============================
# Helpers
# =============================
def safe_col(df: pd.DataFrame, col: str) -> bool:
    return col in df.columns


def first_existing_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def to_date_series(s: pd.Series) -> pd.Series:
    if s is None:
        return pd.Series(dtype="datetime64[ns]")
    return pd.to_datetime(s.astype(str).str.strip(), dayfirst=True, errors="coerce")


def to_datetime_series(s: pd.Series) -> pd.Series:
    if s is None:
        return pd.Series(dtype="datetime64[ns]")

    text = s.astype(str).str.strip()
    iso_mask = text.str.match(r"^\d{4}-\d{2}-\d{2}")
    parsed = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    parsed.loc[iso_mask] = pd.to_datetime(text.loc[iso_mask], yearfirst=True, errors="coerce")
    parsed.loc[~iso_mask] = pd.to_datetime(text.loc[~iso_mask], dayfirst=True, errors="coerce")
    return parsed


def format_number_pt(value: Optional[float], decimals: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    s = f"{float(value):,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_duration_to_minutes(value) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)

    s = str(value).strip().lower()
    if not s:
        return None

    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        parts = [int(x) for x in s.split(":")]
        if len(parts) == 2:
            h, m = parts
            return h * 60 + m
        h, m, sec = parts
        return h * 60 + m + sec / 60.0

    hours = 0.0
    minutes = 0.0
    mh = re.search(r"(\d+(?:[.,]\d+)?)\s*(h|hora|horas)\b", s)
    if mh:
        hours = float(mh.group(1).replace(",", "."))
    mm = re.search(r"(\d+(?:[.,]\d+)?)\s*(m|min|mins|minuto|minutos)\b", s)
    if mm:
        minutes = float(mm.group(1).replace(",", "."))
    if mh or mm:
        return hours * 60 + minutes

    mn = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if mn:
        return float(mn.group(1).replace(",", "."))
    return None


def format_minutes_pt(minutes: Optional[float]) -> str:
    if minutes is None or pd.isna(minutes):
        return "—"
    total_minutes = int(round(max(0, float(minutes))))
    h = total_minutes // 60
    m = total_minutes % 60
    if h > 0 and m > 0:
        return f"{h}h e {m} min"
    if h > 0:
        return f"{h}h"
    return f"{m} min"


def mode_value(series: pd.Series) -> str:
    if series is None:
        return "—"
    s = series.dropna().astype(str).str.strip()
    return "—" if s.empty else s.value_counts().index[0]


def cliente_base(nome: object) -> str:
    if nome is None or (isinstance(nome, float) and pd.isna(nome)):
        return ""
    s = str(nome).strip()
    s = re.sub(r"\s*(?:[-#]|nº|no\.?|num\.?|\.)?\s*\d+\s*$", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", s).strip()


def normalize_status(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip().lower()
    return (
        s.replace("á", "a").replace("à", "a").replace("ã", "a").replace("â", "a")
        .replace("é", "e").replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o").replace("ô", "o").replace("õ", "o")
        .replace("ú", "u").replace("ç", "c")
    )


def is_concluido(status_value: object) -> bool:
    return normalize_status(status_value).startswith("conclu")


def is_cancelado(status_value: object) -> bool:
    return normalize_status(status_value).startswith("cancel")


def is_reagendar(status_value: object) -> bool:
    s = normalize_status(status_value)
    return "reagend" in s or s.startswith("reagendar") or s.startswith("reagendado")


def split_tecnicos(value: object) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    s = re.sub(r"\s*(,|;|/|\\)\s*", ",", s)
    s = re.sub(r"\s+e\s+", ",", s, flags=re.IGNORECASE)
    return [p.strip() for p in s.split(",") if p.strip()]


def month_label_pt(ym: str) -> str:
    meses = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    try:
        y, m = ym.split("-")
        return f"{meses[int(m)-1]}/{y}"
    except Exception:
        return ym


def kpi_card(label: str, value: str, color: str = COR1) -> None:
    st.markdown(
        f"""
        <div style="
            padding: 10px 12px;
            border-radius: 10px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 14px; opacity: 0.85;">{label}</div>
            <div style="font-size: 34px; font-weight: 800; color: {color}; line-height: 1.1;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def modality_card(label: str, average_time: str, quantity: int) -> None:
    st.markdown(
        f"""
        <div style="
            padding: 12px 14px;
            min-height: 118px;
            border-radius: 10px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 15px; font-weight: 700; margin-bottom: 10px;">{escape(label)}</div>
            <div style="font-size: 16px; line-height: 1.45; white-space: nowrap;">
                Tempo Médio: <strong style="color: {COR1};">{escape(average_time)}</strong>
            </div>
            <div style="font-size: 16px; line-height: 1.45; white-space: nowrap;">
                QTD: <strong style="color: {COR1};">{quantity}</strong>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def bar_chart_counts(series: pd.Series, top_n: int, y_label: str) -> None:
    s = series.dropna().astype(str).str.strip()
    counts = s.value_counts().head(top_n)
    if counts.empty:
        st.info("Sem dados para o gráfico.")
        return
    dfc = counts.rename_axis("Categoria").reset_index(name=y_label)
    fig = px.bar(dfc, x="Categoria", y=y_label, text=y_label, template="plotly_dark", color_discrete_sequence=[COR1])
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=20), xaxis_title="", yaxis_title=y_label)
    fig.update_xaxes(tickangle=-35)
    st.plotly_chart(fig, use_container_width=True)


def line_chart_by_day(dates: pd.Series, y_label: str) -> None:
    s = pd.to_datetime(dates, errors="coerce").dropna()
    if s.empty:
        st.info("Sem dados para o gráfico.")
        return
    counts = s.dt.date.value_counts().sort_index()
    dfd = pd.DataFrame({"Data": list(counts.index), y_label: list(counts.values)})
    fig = px.line(dfd, x="Data", y=y_label, markers=True, template="plotly_dark")
    fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=20), xaxis_title="Data", yaxis_title=y_label)
    st.plotly_chart(fig, use_container_width=True)


def histogram_by_hour(time_series: pd.Series, y_label: str) -> None:
    s = time_series.dropna().astype(str).str.strip()
    if s.empty:
        st.info("Sem dados para o gráfico.")
        return

    def _to_hour(x: str) -> Optional[int]:
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", x.strip())
        if m:
            h = int(m.group(1))
            return h if 0 <= h <= 23 else None
        dt = pd.to_datetime(x, errors="coerce")
        return None if pd.isna(dt) else int(dt.hour)

    hours = s.map(_to_hour).dropna().astype(int)
    if hours.empty:
        st.info("Sem horários válidos para o gráfico.")
        return

    counts = hours.value_counts().sort_index()
    dfh = pd.DataFrame({"Hora": list(counts.index), y_label: list(counts.values)})
    fig = px.bar(dfh, x="Hora", y=y_label, template="plotly_dark", color_discrete_sequence=[COR1])
    fig.update_layout(height=360, margin=dict(l=50, r=20, t=20, b=60), xaxis_title="", yaxis_title=y_label)
    fig.update_xaxes(dtick=1)
    st.plotly_chart(fig, use_container_width=True)


def download_csv(df: pd.DataFrame, filename: str) -> None:
    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Baixar CSV filtrado",
        data=csv,
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
    )


def apply_multiselect(df_in: pd.DataFrame, col: str, selected: list[str]) -> pd.DataFrame:
    if not selected or col not in df_in.columns:
        return df_in
    return df_in[df_in[col].astype(str).isin(selected)]


def render_period_filter(
    valid_dates: pd.Series,
    key_prefix: str,
    *,
    include_all: bool = False,
) -> tuple[str, object, object]:
    period_options = ["Este mês", "Este ano", "Personalizado"]
    if include_all:
        period_options.insert(0, "Todo o período")
    period_option = st.radio(
        "Período",
        period_options,
        index=0,
        key=f"{key_prefix}_period",
    )
    start_date = None
    end_date = None

    if period_option == "Personalizado":
        dates = pd.to_datetime(valid_dates, errors="coerce").dropna()
        today_date = pd.Timestamp.today().date()
        default_start = dates.min().date() if not dates.empty else today_date
        default_end = dates.max().date() if not dates.empty else today_date
        selected_start_date = st.date_input(
            "Data inicial",
            value=default_start,
            format="DD/MM/YYYY",
            key=f"{key_prefix}_start_date",
        )
        selected_end_date = st.date_input(
            "Data final",
            value=default_end,
            format="DD/MM/YYYY",
            key=f"{key_prefix}_end_date",
        )

        applied_start_key = f"{key_prefix}_applied_start_date"
        applied_end_key = f"{key_prefix}_applied_end_date"
        if applied_start_key not in st.session_state:
            st.session_state[applied_start_key] = default_start
        if applied_end_key not in st.session_state:
            st.session_state[applied_end_key] = default_end

        if st.button("Filtrar", use_container_width=True, key=f"{key_prefix}_apply_period"):
            if selected_start_date > selected_end_date:
                st.error("A data inicial deve ser anterior ou igual à data final.")
            else:
                st.session_state[applied_start_key] = selected_start_date
                st.session_state[applied_end_key] = selected_end_date

        start_date = st.session_state.get(applied_start_key)
        end_date = st.session_state.get(applied_end_key)

    return period_option, start_date, end_date


def apply_period_filter(
    df_in: pd.DataFrame,
    date_col: str,
    period_option: str,
    start_date: object = None,
    end_date: object = None,
) -> pd.DataFrame:
    if date_col not in df_in.columns or not df_in[date_col].notna().any():
        return df_in

    df_out = df_in.copy()
    dates = pd.to_datetime(df_out[date_col], errors="coerce")
    today_date = pd.Timestamp.today().date()
    if period_option == "Este mês":
        mask = (dates.dt.year == today_date.year) & (dates.dt.month == today_date.month)
    elif period_option == "Este ano":
        mask = dates.dt.year == today_date.year
    elif start_date is not None and end_date is not None:
        if start_date > end_date:
            return df_out.iloc[0:0]
        mask = (dates.dt.date >= start_date) & (dates.dt.date <= end_date)
    else:
        mask = pd.Series(True, index=df_out.index)

    return df_out[mask]


@st.cache_data(ttl=300, show_spinner=False)
def sheet_column_options(
    spreadsheet_id: str,
    sheet_name: str,
    column_name: str,
    fallback: tuple[str, ...],
    include_blank: bool = True,
) -> list[str]:
    try:
        df = read_sheet(spreadsheet_id, sheet_name)
        if column_name in df.columns:
            options = (
                df[column_name]
                .dropna()
                .astype(str)
                .str.strip()
                .loc[lambda s: s.ne("")]
                .drop_duplicates()
                .tolist()
            )
            if options:
                return ([""] if include_blank else []) + options
    except Exception:
        pass

    return ([""] if include_blank else []) + list(fallback)


def sim_nao_to_bool_text(value: str) -> str:
    return "TRUE" if value == "SIM" else "FALSE"


# =============================
# Configs dos módulos
# =============================
INSTALLATIONS_SHEET = "Instalacoes_2026"
TECH_VISITS_SHEET = "Atendimentos_Tecnicos_2026"

MODULES = {
    "INSTALAÇÕES": {
        "sheet": INSTALLATIONS_SHEET,
        "kind": "instalacoes",
        "item_label": "Instalações",
        "item_singular": "Instalação",
        "completed_label": "Instalações Concluídas",
        "plural_article": "das",
        "status_col_candidates": ["Status", "Status da Instalação", "Status Instalação", "Situacao", "Situação"],
        "reason_col_candidates": [
            "Motivo reagendamento",
            "Motivo Reagendamento",
            "Motivo do reagendamento",
            "Motivo do Reagendamento",
            "Motivo",
        ],
        "extra_filter_candidates": [("Plano", ["Plano"])],
        "csv_name": "relatorio_instalacoes_filtrado.csv",
    },
    "ATENDIMENTOS TÉCNICOS": {
        "sheet": TECH_VISITS_SHEET,
        "kind": "visitas",
        "item_label": "Atendimentos Técnicos",
        "item_singular": "Atendimento Técnico",
        "completed_label": "Atendimentos Técnicos Concluídos",
        "plural_article": "dos",
        "status_col_candidates": ["Status"],
        "reason_col_candidates": ["Motivo do Atendimento", "Motivo Reagendamento", "Motivo"],
        "extra_filter_candidates": [
            ("Tipo de Atendimento", ["Tipo de Atendimento"]),
            ("Sistema", ["Sistema"]),
            ("Contratual", ["Contratual"]),
        ],
        "csv_name": "relatorio_atendimentos_tecnicos_filtrado.csv",
    },
}


# =============================
# Estado / Navegação
# =============================
if "view_mode" not in st.session_state:
    st.session_state.view_mode = "GERAL"

SPREADSHEET_ID = st.secrets.get("spreadsheet_id", "")
if not SPREADSHEET_ID:
    st.error("Faltou configurar `spreadsheet_id` nos secrets.")
    st.stop()

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), use_container_width=True)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    if st.button("GERAL", use_container_width=True):
        st.session_state.view_mode = "GERAL"
    if st.button("TIFLUX", use_container_width=True):
        st.session_state.view_mode = "TIFLUX"
    if st.button("INSTALAÇÕES", use_container_width=True):
        st.session_state.view_mode = "INSTALAÇÕES"
    if st.button("ATENDIMENTOS", use_container_width=True):
        st.session_state.view_mode = "ATENDIMENTOS TÉCNICOS"
    if st.button("CADASTRAR", use_container_width=True):
        st.session_state.view_mode = "CADASTRAR"
    st.divider()


# =============================
# Helpers de formulário
# =============================
def _digits_only(x: str) -> str:
    return re.sub(r"\D", "", x or "")


def _mask_date_ddmmyyyy(x: str) -> str:
    d = _digits_only(x)[:8]
    if len(d) <= 2:
        return d
    if len(d) <= 4:
        return f"{d[:2]}/{d[2:4]}"
    return f"{d[:2]}/{d[2:4]}/{d[4:8]}"


def _mask_time_hhmm(x: str) -> str:
    d = _digits_only(x)[:4]
    if len(d) <= 2:
        return d
    return f"{d[:2]}:{d[2:4]}"


def _parse_date_ddmmyyyy(s: str):
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{8}", s):
        s = _mask_date_ddmmyyyy(s)
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        return None


def _parse_time_hhmm(s: str):
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}", s):
        s = _mask_time_hhmm(s)
    if not re.fullmatch(r"\d{2}:\d{2}", s):
        return None
    try:
        return datetime.strptime(s, "%H:%M").time()
    except Exception:
        return None


def _duration_hhmm(start_hhmm: str, end_hhmm: str) -> str:
    t1 = _parse_time_hhmm(start_hhmm)
    t2 = _parse_time_hhmm(end_hhmm)
    if not t1 or not t2:
        raise ValueError("Horário inválido para cálculo de duração.")
    dt1 = datetime.combine(datetime.today().date(), t1)
    dt2 = datetime.combine(datetime.today().date(), t2)
    if dt2 < dt1:
        raise ValueError("Término não pode ser menor que Início.")
    diff: timedelta = dt2 - dt1
    total_minutes = int(diff.total_seconds() // 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


# =============================
# Formulários
# =============================
def render_installation_form(show_title: bool = True) -> None:
    if show_title:
        st.title("CADASTRAR INSTALAÇÃO")
    else:
        st.subheader("INSTALAÇÃO")

    def _on_data_change():
        st.session_state.inst_data_txt = _mask_date_ddmmyyyy(st.session_state.get("inst_data_txt", ""))

    def _on_inicio_change():
        st.session_state.inst_inicio_txt = _mask_time_hhmm(st.session_state.get("inst_inicio_txt", ""))

    def _on_termino_change():
        st.session_state.inst_termino_txt = _mask_time_hhmm(st.session_state.get("inst_termino_txt", ""))

    c1, c2, c3 = st.columns(3)
    with c1:
        data_txt = st.text_input("Data", placeholder="dd/mm/aaaa", key="inst_data_txt", max_chars=10, on_change=_on_data_change)
        inicio_txt = st.text_input("Início", placeholder="hh:mm", key="inst_inicio_txt", max_chars=5, on_change=_on_inicio_change)
        termino_txt = st.text_input("Término", placeholder="hh:mm", key="inst_termino_txt", max_chars=5, on_change=_on_termino_change)
    with c2:
        modalidade = st.selectbox("Modalidade", ["Remota", "Presencial", "Híbrida", "Evento", "Apresentação", "Boas-vindas"])
        consultor = st.selectbox("Consultor", ["Shimada", "André", "Jefferson", "Sandro", "Renato", "Tiago"])
        tecnicos_sel = st.multiselect("Técnico(s)", ["Davi", "Vinícius", "Marcos", "Ryen", "Jonathan", "Renato", "Fábio"], default=[])
    with c3:
        status = st.selectbox("Status", ["Concluído", "Cancelado", "Reagendar"])
        uf_txt = st.text_input("UF", placeholder="SP", max_chars=2)
        cidade_txt = st.text_input("Cidade")

    st.divider()

    c4, c5, c6 = st.columns(3)
    with c4:
        cliente_txt = st.text_input("Cliente")
        cv_txt = st.text_input("CV")
        cv_inst_txt = st.text_input("CV Instalação (código)")
    with c5:
        emissor_tipo = st.selectbox("Emissor de senhas", ["Quiosque de chão", "Quiosque de mesa", "Portátil", "Software", "Sem emissor"])
        emissor_cliente = st.selectbox("Emissor fornecido pelo cliente?", ["NÃO", "SIM"])
        emissores_qtd = st.number_input("Emissores (quantidade)", min_value=0, step=1, value=0)
    with c6:
        player_tipo = st.selectbox("Player", ["Stick Player", "MiniPC", "Software", "Sem player"])
        player_cliente = st.selectbox("Player fornecido pelo cliente?", ["NÃO", "SIM"])
        players_qtd = st.number_input("Players (quantidade)", min_value=0, step=1, value=0)

    st.divider()

    c7, c8 = st.columns(2)
    with c7:
        plano = st.selectbox("Plano", ["", "TB", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T15", "Locação"], index=0)
    with c8:
        motivo_reag = st.selectbox("Motivo reagendamento", ["", "Finalizar treinamento", "Finalizar instalação", "Infraestrutura", "Stick", "Totem", "Cancelamento"], index=0)

    observacao_txt = st.text_area("Observação")

    if st.button("Salvar na planilha", use_container_width=True):
        errors = []
        d = _parse_date_ddmmyyyy(data_txt)
        if not d:
            errors.append("Data inválida (use dd/mm/aaaa).")
        if data_txt.strip() and not re.fullmatch(r"[0-9/]+", data_txt.strip()):
            errors.append("Data: use apenas números e '/'.")
        if _parse_time_hhmm(inicio_txt.strip()) is None:
            errors.append("Início inválido (use HH:MM).")
        if _parse_time_hhmm(termino_txt.strip()) is None:
            errors.append("Término inválido (use HH:MM).")
        if inicio_txt.strip() and not re.fullmatch(r"[0-9:]+", inicio_txt.strip()):
            errors.append("Início: use apenas números e ':'.")
        if termino_txt.strip() and not re.fullmatch(r"[0-9:]+", termino_txt.strip()):
            errors.append("Término: use apenas números e ':'.")
        uf_clean = (uf_txt or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", uf_clean):
            errors.append("UF inválida (use 2 letras, ex.: SP).")
        duracao_calc = None
        if not errors:
            try:
                duracao_calc = _duration_hhmm(inicio_txt.strip(), termino_txt.strip())
            except Exception as ex:
                errors.append(str(ex))

        if errors:
            for e in errors:
                st.error(e)
            return

        values_by_header = {
            "Data": d.strftime("%d/%m/%Y"),
            "Início": inicio_txt.strip(),
            "Término": termino_txt.strip(),
            "Modalidade": modalidade,
            "Consultor": consultor,
            "Cliente": cliente_txt.strip(),
            "Emissor de senhas": emissor_tipo,
            "Emissor cliente": sim_nao_to_bool_text(emissor_cliente),
            "Emissores": int(emissores_qtd),
            "Quantidade Quiosque": int(emissores_qtd),
            "Player": player_tipo,
            "Player cliente": sim_nao_to_bool_text(player_cliente),
            "Players": int(players_qtd),
            "Quantidade Players": int(players_qtd),
            "UF": uf_clean,
            "Cidade": cidade_txt.strip(),
            "Técnico": ", ".join(tecnicos_sel) if tecnicos_sel else "",
            "Status": status,
            "CV": cv_txt.strip(),
            "Plano": plano,
            "CV Instalação": cv_inst_txt.strip(),
            "Motivo reagendamento": motivo_reag.strip(),
            "Observação": observacao_txt.strip(),
            "Duração": duracao_calc or "",
        }

        try:
            append_row_to_sheet(SPREADSHEET_ID, INSTALLATIONS_SHEET, values_by_header)
            st.success("✅ Registro salvo na planilha!")
        except DuplicateRowError as ex:
            st.warning(str(ex))
        except Exception as ex:
            st.error(f"Não foi possível salvar na planilha: {ex}")


def render_visit_form(show_title: bool = True) -> None:
    if show_title:
        st.title("CADASTRAR ATENDIMENTO TÉCNICO")
    else:
        st.subheader("ATENDIMENTO TÉCNICO")

    def _on_data_change():
        st.session_state.visit_data_txt = _mask_date_ddmmyyyy(st.session_state.get("visit_data_txt", ""))

    def _on_inicio_change():
        st.session_state.visit_inicio_txt = _mask_time_hhmm(st.session_state.get("visit_inicio_txt", ""))

    def _on_termino_change():
        st.session_state.visit_termino_txt = _mask_time_hhmm(st.session_state.get("visit_termino_txt", ""))

    modalidade_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Modalidade",
        ("Remota", "Presencial", "Híbrida", "Evento", "Apresentação", "Boas-vindas"),
    )
    consultor_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Consultor",
        ("Shimada", "André", "Jefferson", "Sandro", "Renato"),
    )
    tecnico_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Técnico",
        ("Davi", "Vinícius", "Marcos", "Ryen", "Jonathan", "Renato", "Fábio"),
        include_blank=False,
    )
    tipo_atendimento_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Tipo de Atendimento",
        ("Treinamento", "Suporte", "Manutenção", "Visita técnica"),
    )
    tipo_treinamento_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Tipo de treinamento",
        ("Operacional", "Administrativo", "Reciclagem"),
    )
    sistema_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Sistema",
        ("NextQS", "NextCall", "NextTotem"),
    )
    status_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Status",
        ("Concluído", "Cancelado", "Reagendar"),
    )
    equipamento_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Equipamento",
        ("Emissor", "Player", "TV", "Totem", "Software"),
    )
    contratual_options = sheet_column_options(
        SPREADSHEET_ID,
        TECH_VISITS_SHEET,
        "Contratual",
        ("Sim", "Não"),
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        data_txt = st.text_input("Data", placeholder="dd/mm/aaaa", key="visit_data_txt", max_chars=10, on_change=_on_data_change)
        inicio_txt = st.text_input("Início", placeholder="hh:mm", key="visit_inicio_txt", max_chars=5, on_change=_on_inicio_change)
        termino_txt = st.text_input("Término", placeholder="hh:mm", key="visit_termino_txt", max_chars=5, on_change=_on_termino_change)
    with c2:
        modalidade = st.selectbox("Modalidade", modalidade_options)
        cliente = st.text_input("Cliente")
        consultor = st.selectbox("Consultor", consultor_options)
    with c3:
        uf_txt = st.text_input("UF", placeholder="SP", max_chars=2)
        cidade = st.text_input("Cidade")
        tecnicos_sel = st.multiselect("Técnico(s)", tecnico_options, default=[])

    st.divider()

    c4, c5, c6 = st.columns(3)
    with c4:
        terceiro = st.selectbox("Terceiro no local", ["NÃO", "SIM"])
        tipo_atendimento = st.selectbox("Tipo de Atendimento", tipo_atendimento_options)
        equipamento = st.selectbox("Equipamento", equipamento_options)
    with c5:
        tipo_treinamento = ""
        if tipo_atendimento.strip().casefold() == "treinamento":
            tipo_treinamento = st.selectbox("Tipo de treinamento", tipo_treinamento_options)
        sistema = st.selectbox("Sistema", sistema_options)
        contratual = st.selectbox("Contratual", contratual_options)
    with c6:
        status = st.selectbox("Status", status_options)
        motivo = st.text_input("Motivo do Atendimento")

    motivo_reagendamento = st.text_input("Motivo Reagendamento")
    observacao = st.text_area("Observação")

    if st.button("Salvar na planilha", use_container_width=True, key="save_visit"):
        errors = []
        d = _parse_date_ddmmyyyy(data_txt)
        if not d:
            errors.append("Data inválida (use dd/mm/aaaa).")
        if _parse_time_hhmm(inicio_txt.strip()) is None:
            errors.append("Início inválido (use HH:MM).")
        if _parse_time_hhmm(termino_txt.strip()) is None:
            errors.append("Término inválido (use HH:MM).")
        uf_clean = (uf_txt or "").strip().upper()
        if uf_clean and not re.fullmatch(r"[A-Z]{2}", uf_clean):
            errors.append("UF inválida (use 2 letras, ex.: SP).")
        duracao_calc = None
        if not errors:
            try:
                duracao_calc = _duration_hhmm(inicio_txt.strip(), termino_txt.strip())
            except Exception as ex:
                errors.append(str(ex))

        if errors:
            for e in errors:
                st.error(e)
            return

        values_by_header = {
            "Data": d.strftime("%d/%m/%Y"),
            "Início": inicio_txt.strip(),
            "Término": termino_txt.strip(),
            "Modalidade": modalidade,
            "Cliente": cliente.strip(),
            "Consultor": consultor,
            "UF": uf_clean,
            "Cidade": cidade.strip(),
            "Técnico": ", ".join(tecnicos_sel) if tecnicos_sel else "",
            "Terceiro no local": sim_nao_to_bool_text(terceiro),
            "Tipo de Atendimento": tipo_atendimento,
            "Equipamento": equipamento,
            "Tipo de treinamento": tipo_treinamento,
            "Sistema": sistema,
            "Contratual": contratual,
            "Status": status,
            "Motivo do Atendimento": motivo.strip(),
            "Motivo Reagendamento": motivo_reagendamento.strip(),
            "Observação": observacao.strip(),
            "Duração": duracao_calc or "",
        }

        try:
            append_row_to_sheet(SPREADSHEET_ID, TECH_VISITS_SHEET, values_by_header)
            st.success("✅ Registro salvo na planilha!")
        except DuplicateRowError as ex:
            st.warning(str(ex))
        except Exception as ex:
            st.error(f"Não foi possível salvar na planilha: {ex}")


def render_register_page() -> None:
    st.title("CADASTRAR")
    register_type = st.radio(
        "Tipo de cadastro",
        ["ATENDIMENTO TÉCNICO", "INSTALAÇÃO"],
        horizontal=True,
        key="register_type",
    )
    st.divider()

    if register_type == "ATENDIMENTO TÉCNICO":
        render_visit_form(show_title=False)
    else:
        render_installation_form(show_title=False)


def _module_metrics(df_raw: pd.DataFrame, cfg: dict[str, object]) -> dict[str, object]:
    df = df_raw.copy()
    df.columns = df.columns.str.strip()

    status_col = first_existing_col(df, cfg["status_col_candidates"])
    item_label = str(cfg["item_label"])
    completed_label = str(cfg["completed_label"])

    concluidas = int(df[status_col].map(is_concluido).sum()) if status_col else 0
    mins = df["Duração"].map(_parse_duration_to_minutes).dropna() if safe_col(df, "Duração") else pd.Series(dtype=float)
    tempo_medio_str = format_minutes_pt(mins.mean()) if not mins.empty else "—"
    modalidade_mais_comum = mode_value(df["Modalidade"]) if safe_col(df, "Modalidade") else "—"
    reag_rate = float(df[status_col].map(is_reagendar).mean()) if status_col and len(df) else None
    taxa_reag = f"{reag_rate*100:.1f}%" if reag_rate is not None else "—"
    taxa_reag_color = COR2 if (reag_rate is not None and reag_rate >= 0.26) else COR1

    total_minutes_sum = float(mins.sum()) if not mins.empty else 0.0
    horas_totais = total_minutes_sum / 60.0 if total_minutes_sum else 0.0

    cliente_mais = "—"
    if safe_col(df, "Cliente"):
        tmpc = df["Cliente"].map(cliente_base)
        tmpc = tmpc.dropna().astype(str).str.strip()
        tmpc = tmpc[tmpc != ""]
        if not tmpc.empty:
            cliente_mais = tmpc.value_counts().index[0]

    return {
        "item_label": item_label,
        "completed_label": completed_label,
        "concluidas": concluidas,
        "tempo_medio": tempo_medio_str,
        "modalidade_mais_comum": modalidade_mais_comum,
        "taxa_reag": taxa_reag,
        "taxa_reag_color": taxa_reag_color,
        "horas_totais": horas_totais,
        "cliente_mais": cliente_mais,
    }


def render_highlight_cards(df_raw: pd.DataFrame, cfg: dict[str, object]) -> None:
    metrics = _module_metrics(df_raw, cfg)
    item_label = str(metrics["item_label"])
    completed_label = str(metrics["completed_label"])

    k1, k2, k3 = st.columns(3)
    with k1:
        kpi_card(completed_label, str(metrics["concluidas"]), COR1)
    with k2:
        kpi_card("Tempo Médio", str(metrics["tempo_medio"]), COR1)
    with k3:
        kpi_card("Modalidade mais comum", str(metrics["modalidade_mais_comum"]), COR1)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    k4, k5, k6 = st.columns(3)
    with k4:
        kpi_card("Taxa de Reagendamentos", str(metrics["taxa_reag"]), str(metrics["taxa_reag_color"]))
    with k5:
        horas_totais = float(metrics["horas_totais"])
        kpi_card("Horas Totais", f"{format_number_pt(horas_totais, 1)} h" if horas_totais else "0,0 h", COR1)
    with k6:
        kpi_card(f"Cliente com mais {item_label.lower()}", str(metrics["cliente_mais"]), COR1)


def render_average_time_by_modality(df_raw: pd.DataFrame, item_label: str) -> None:
    if df_raw.empty or not safe_col(df_raw, "Modalidade") or not safe_col(df_raw, "Duração"):
        st.info("Sem dados suficientes para calcular o tempo médio por modalidade.")
        return

    df = df_raw.copy()
    df["_minutos"] = df["Duração"].map(_parse_duration_to_minutes)
    df["_modalidade"] = df["Modalidade"].fillna("").astype(str).str.strip()
    df = df[df["_modalidade"] != ""]
    if df.empty:
        st.info("Sem modalidades preenchidas para exibir.")
        return

    summary = (
        df.groupby("_modalidade", dropna=False)
        .agg(Minutos=("_minutos", "mean"), QTD=("_modalidade", "size"))
        .sort_values(["QTD", "Minutos"], ascending=[False, False])
        .reset_index()
        .rename(columns={"_modalidade": "Modalidade"})
    )
    summary["Tempo médio"] = summary["Minutos"].map(format_minutes_pt)

    cols_per_row = 4
    for start in range(0, len(summary), cols_per_row):
        if start > 0:
            st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        cols = st.columns(cols_per_row)
        for col, (_, row) in zip(cols, summary.iloc[start : start + cols_per_row].iterrows()):
            with col:
                modality_card(str(row["Modalidade"]), str(row["Tempo médio"]), int(row["QTD"]))


def _prepare_general_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = df_raw.copy()
    df.columns = df.columns.str.strip()
    df["_data"] = to_date_series(df["Data"]) if safe_col(df, "Data") else pd.NaT
    if safe_col(df, "Cliente"):
        df["_cliente_base"] = df["Cliente"].map(cliente_base)
    return df


def _general_options(dfs: list[pd.DataFrame], col: str) -> list[str]:
    values = []
    for df in dfs:
        if safe_col(df, col):
            values.extend(df[col].dropna().astype(str).str.strip().tolist())
    return sorted({v for v in values if v})


def _general_people_options(dfs: list[pd.DataFrame], candidates: list[str], split_values: bool = False) -> list[str]:
    values = []
    for df in dfs:
        col = first_existing_col(df, candidates)
        if not col:
            continue
        if split_values:
            values.extend(df[col].map(split_tecnicos).explode().dropna().astype(str).str.strip().tolist())
        else:
            values.extend(df[col].dropna().astype(str).str.strip().tolist())
    return sorted({v for v in values if v})


def _apply_general_filters(df: pd.DataFrame, filters: dict[str, object]) -> pd.DataFrame:
    df_f = apply_period_filter(
        df,
        "_data",
        str(filters["period_option"]),
        filters.get("start_date"),
        filters.get("end_date"),
    )

    df_f = apply_multiselect(df_f, "Modalidade", filters["sel_modalidade"])
    df_f = apply_multiselect(df_f, "UF", filters["sel_uf"])
    df_f = apply_multiselect(df_f, "Cidade", filters["sel_cidade"])

    sel_cliente = filters["sel_cliente"]
    if sel_cliente and safe_col(df_f, "_cliente_base"):
        df_f = df_f[df_f["_cliente_base"].astype(str).isin([str(x) for x in sel_cliente])]

    tecnico_col = first_existing_col(df_f, ["Técnico", "Tecnico", "Técnicos", "Tecnicos"])
    sel_tecnico = filters["sel_tecnico"]
    if tecnico_col and sel_tecnico:
        sel_set = set(str(x).strip() for x in sel_tecnico if str(x).strip())
        df_f = df_f[df_f[tecnico_col].map(lambda v: bool(set(split_tecnicos(v)) & sel_set))]

    consultor_col = first_existing_col(df_f, ["Consultor", "Consultores", "Consultor(a)"])
    sel_consultor = filters["sel_consultor"]
    if consultor_col and sel_consultor:
        df_f = apply_multiselect(df_f, consultor_col, sel_consultor)

    return df_f


def render_general_dashboard() -> None:
    prepared = {}
    for module_key in ["INSTALAÇÕES", "ATENDIMENTOS TÉCNICOS"]:
        cfg = MODULES[module_key]
        prepared[module_key] = _prepare_general_df(read_sheet(SPREADSHEET_ID, cfg["sheet"]))

    dfs = list(prepared.values())
    valid_dates = pd.concat([df["_data"].dropna() for df in dfs if safe_col(df, "_data")], ignore_index=True)

    with st.sidebar:
        st.header("Filtros")
        period_option, start_date, end_date = render_period_filter(valid_dates, "general")

        with st.expander("Filtros avançados", expanded=False):
            sel_modalidade = st.multiselect("Modalidade", options=_general_options(dfs, "Modalidade"), key="general_modalidade")
            sel_uf = st.multiselect("UF", options=_general_options(dfs, "UF"), key="general_uf")
            sel_cidade = st.multiselect("Cidade", options=_general_options(dfs, "Cidade"), key="general_cidade")
            cliente_opts = sorted({v for df in dfs if safe_col(df, "_cliente_base") for v in df["_cliente_base"].dropna().astype(str).str.strip().tolist() if v})
            sel_cliente = st.multiselect("Cliente", options=cliente_opts, key="general_cliente")
            sel_tecnico = st.multiselect(
                "Técnico",
                options=_general_people_options(dfs, ["Técnico", "Tecnico", "Técnicos", "Tecnicos"], split_values=True),
                key="general_tecnico",
            )
            sel_consultor = st.multiselect(
                "Consultor",
                options=_general_people_options(dfs, ["Consultor", "Consultores", "Consultor(a)"]),
                key="general_consultor",
            )

    filters = {
        "period_option": period_option,
        "start_date": start_date,
        "end_date": end_date,
        "sel_modalidade": sel_modalidade,
        "sel_uf": sel_uf,
        "sel_cidade": sel_cidade,
        "sel_cliente": sel_cliente,
        "sel_tecnico": sel_tecnico,
        "sel_consultor": sel_consultor,
    }

    for module_key in ["INSTALAÇÕES", "ATENDIMENTOS TÉCNICOS"]:
        cfg = MODULES[module_key]
        item_label = str(cfg["item_label"])
        st.subheader(item_label)

        df_raw = _apply_general_filters(prepared[module_key], filters)
        if df_raw.empty:
            st.warning(f"Nenhum registro de {item_label.lower()} encontrado para os filtros selecionados.")
        else:
            render_highlight_cards(df_raw, cfg)
            st.subheader("Modalidades")
            render_average_time_by_modality(df_raw, item_label)

        if module_key != "ATENDIMENTOS TÉCNICOS":
            st.divider()


# =============================
# Dashboard genérico
# =============================
def render_dashboard(module_key: str) -> None:
    cfg = MODULES[module_key]
    st.title(cfg["item_label"].upper())

    df_raw = read_sheet(SPREADSHEET_ID, cfg["sheet"])
    if df_raw.empty:
        st.warning("A planilha não retornou dados.")
        return

    df = df_raw.copy()
    df.columns = df.columns.str.strip()
    df["_data"] = to_date_series(df["Data"]) if safe_col(df, "Data") else pd.NaT
    has_valid_dates = df["_data"].notna().any()

    status_col = first_existing_col(df, cfg["status_col_candidates"])
    reason_col = first_existing_col(df, cfg["reason_col_candidates"])
    tecnico_col = first_existing_col(df, ["Técnico", "Tecnico", "Técnicos", "Tecnicos"])
    consultor_col = first_existing_col(df, ["Consultor", "Consultores", "Consultor(a)"])

    with st.sidebar:
        st.header("Filtros")
        period_option, start_date, end_date = render_period_filter(
            df["_data"].dropna(),
            str(cfg["kind"]),
        )

        with st.expander("Filtros avançados", expanded=False):
            def multiselect_filter(label: str, col: str) -> list[str]:
                if not safe_col(df, col):
                    return []
                vals = sorted(df[col].dropna().astype(str).unique().tolist())
                return st.multiselect(label, options=vals)

            sel_modalidade = multiselect_filter("Modalidade", "Modalidade")
            sel_uf = multiselect_filter("UF", "UF")
            sel_cidade = multiselect_filter("Cidade", "Cidade")

            if safe_col(df, "Cliente"):
                df["_cliente_base"] = df["Cliente"].map(cliente_base)
                cliente_opts = sorted([c for c in df["_cliente_base"].dropna().astype(str).unique().tolist() if c.strip()])
                sel_cliente = st.multiselect("Cliente", options=cliente_opts)
            else:
                sel_cliente = []

            extra_filters = {}
            for label, candidates in cfg["extra_filter_candidates"]:
                col = first_existing_col(df, candidates)
                extra_filters[label] = (col, multiselect_filter(label, col) if col else [])

            sel_tecnico = multiselect_filter("Técnico", tecnico_col) if tecnico_col else []
            sel_consultor = multiselect_filter("Consultor", consultor_col) if consultor_col and consultor_col != tecnico_col else []

            show_cols = st.multiselect(
                "Colunas na tabela",
                options=[c for c in df.columns if not c.startswith("_")],
                default=[c for c in df.columns if not c.startswith("_")],
            )

    df_f = apply_period_filter(df, "_data", period_option, start_date, end_date)

    df_f = apply_multiselect(df_f, "Modalidade", sel_modalidade)
    df_f = apply_multiselect(df_f, "UF", sel_uf)
    df_f = apply_multiselect(df_f, "Cidade", sel_cidade)

    if sel_cliente and safe_col(df_f, "Cliente"):
        df_f["_cliente_base"] = df_f["Cliente"].map(cliente_base)
        df_f = df_f[df_f["_cliente_base"].astype(str).isin([str(x) for x in sel_cliente])]

    for _, (col, selected) in extra_filters.items():
        if col:
            df_f = apply_multiselect(df_f, col, selected)

    if tecnico_col and sel_tecnico:
        sel_set = set(str(x).strip() for x in sel_tecnico if str(x).strip())
        df_f = df_f[df_f[tecnico_col].map(lambda v: bool(set(split_tecnicos(v)) & sel_set))]

    if consultor_col and sel_consultor:
        df_f = apply_multiselect(df_f, consultor_col, sel_consultor)

    item_label = cfg["item_label"]
    item_singular = cfg["item_singular"]
    chart_y = item_label

    concluidas = int(df_f[status_col].map(is_concluido).sum()) if status_col else 0
    mins = df_f["Duração"].map(_parse_duration_to_minutes).dropna() if safe_col(df_f, "Duração") else pd.Series(dtype=float)
    tempo_medio_str = format_minutes_pt(mins.mean()) if not mins.empty else "—"
    modalidade_mais_comum = mode_value(df_f["Modalidade"]) if safe_col(df_f, "Modalidade") else "—"
    reag_rate = float(df_f[status_col].map(is_reagendar).mean()) if status_col and len(df_f) else None
    taxa_reag = f"{reag_rate*100:.1f}%" if reag_rate is not None else "—"
    taxa_reag_color = COR2 if (reag_rate is not None and reag_rate >= 0.26) else COR1

    k1, k2, k3 = st.columns(3)
    with k1:
        kpi_card(str(cfg["completed_label"]), str(concluidas), COR1)
    with k2:
        kpi_card("Tempo Médio", tempo_medio_str, COR1)
    with k3:
        kpi_card("Modalidade mais comum", modalidade_mais_comum, COR1)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    total_minutes_sum = float(mins.sum()) if not mins.empty else 0.0
    horas_totais = total_minutes_sum / 60.0 if total_minutes_sum else 0.0

    cliente_mais = "—"
    if safe_col(df_f, "Cliente"):
        tmpc = df_f["Cliente"].map(cliente_base)
        tmpc = tmpc.dropna().astype(str).str.strip()
        tmpc = tmpc[tmpc != ""]
        if not tmpc.empty:
            cliente_mais = tmpc.value_counts().index[0]

    k4, k5, k6 = st.columns(3)
    with k4:
        kpi_card("Taxa de Reagendamentos", taxa_reag, taxa_reag_color)
    with k5:
        kpi_card("Horas Totais", f"{format_number_pt(horas_totais, 1)} h" if horas_totais else "0,0 h", COR1)
    with k6:
        kpi_card(f"Cliente com mais {item_label.lower()}", cliente_mais, COR1)

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    st.subheader(f"{item_label} por dia")
    if has_valid_dates and df_f["_data"].notna().any():
        line_chart_by_day(df_f["_data"], chart_y)
    else:
        st.info("Sem coluna de data válida para série temporal.")

    if has_valid_dates and df_f["_data"].notna().any():
        months = df_f["_data"].dt.to_period("M").dropna()
        if months.nunique() > 1:
            st.subheader(f"Meses com mais {item_label}")
            counts = months.astype(str).value_counts().sort_values(ascending=False)
            dfm = counts.rename_axis("Mês").reset_index(name=chart_y)
            dfm["Mês (rótulo)"] = dfm["Mês"].map(month_label_pt)
            fig = px.bar(dfm, x="Mês", y=chart_y, text=chart_y, template="plotly_dark", color_discrete_sequence=[COR1])
            fig.update_traces(textposition="outside", cliponaxis=False)
            fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=20), xaxis_title="", yaxis_title=chart_y)
            fig.update_xaxes(tickangle=-35, tickmode="array", tickvals=dfm["Mês"].tolist(), ticktext=dfm["Mês (rótulo)"].tolist())
            st.plotly_chart(fig, use_container_width=True)

    g1, g2 = st.columns(2)
    with g1:
        st.subheader(f"{item_label} por Modalidade")
        if safe_col(df_f, "Modalidade"):
            bar_chart_counts(df_f["Modalidade"], top_n=20, y_label=chart_y)
        else:
            st.info("Coluna 'Modalidade' não encontrada.")
    with g2:
        st.subheader(f"Status — {item_label}")
        if status_col:
            s_bucket = df_f[status_col].dropna().astype(str).str.strip()
            if s_bucket.empty:
                st.info("Sem dados de status para o gráfico.")
            else:
                counts = pd.Series(
                    {
                        "Concluído": int(df_f[status_col].map(is_concluido).sum()),
                        "Reagendar": int(df_f[status_col].map(is_reagendar).sum()),
                        "Cancelado": int(df_f[status_col].map(is_cancelado).sum()),
                    }
                )
                counts = counts[counts > 0]
                dfc = counts.rename_axis("Status").reset_index(name=chart_y)
                fig = px.bar(dfc, x="Status", y=chart_y, text=chart_y, template="plotly_dark", color_discrete_sequence=[COR1])
                fig.update_traces(textposition="outside", cliponaxis=False)
                fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=20), xaxis_title="", yaxis_title=chart_y)
                fig.update_xaxes(tickangle=-20)
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Coluna de status não encontrada.")

    c_left, c_right = st.columns(2)
    with c_left:
        st.subheader(f"{item_label} por Técnico")
        if tecnico_col:
            tech_series = df_f[tecnico_col].map(split_tecnicos).explode().dropna().astype(str).str.strip()
            tech_series = tech_series[tech_series != ""]
            if tech_series.empty:
                st.info("Sem dados de técnico para o gráfico.")
            else:
                tech_counts = tech_series.value_counts().reset_index()
                tech_counts.columns = ["Técnico", chart_y]
                fig = px.bar(tech_counts, x="Técnico", y=chart_y, text=chart_y, template="plotly_dark", color_discrete_sequence=[COR1])
                fig.update_traces(textposition="outside", cliponaxis=False)
                fig.update_layout(height=360, margin=dict(l=50, r=20, t=20, b=60), xaxis_title="", yaxis_title=chart_y)
                fig.update_xaxes(tickangle=-35)
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Coluna de Técnico não encontrada.")
    with c_right:
        st.subheader(f"Horário {cfg['plural_article']} {item_label}")
        if safe_col(df_f, "Início"):
            histogram_by_hour(df_f["Início"], y_label=chart_y)
        else:
            st.info("Coluna de horário inicial não encontrada.")

    cl_left, cl_right = st.columns(2)
    with cl_left:
        st.subheader(f"Clientes e quantidade de {item_label}")
        if safe_col(df_f, "Cliente"):
            tmp = df_f.copy()
            tmp["_cliente_base"] = tmp["Cliente"].map(cliente_base)
            base = tmp["_cliente_base"].dropna().astype(str).str.strip()
            base = base[base != ""]
            tmp = tmp.loc[base.index].copy()
            if tmp.empty:
                st.info("Sem dados de cliente para listar.")
            else:
                if status_col:
                    tmp["_is_concluido"] = tmp[status_col].map(is_concluido)
                    tmp["_is_reagendar"] = tmp[status_col].map(is_reagendar)
                    tmp["_is_cancelado"] = tmp[status_col].map(is_cancelado)
                else:
                    tmp["_is_concluido"] = False
                    tmp["_is_reagendar"] = False
                    tmp["_is_cancelado"] = False
                df_clientes = (
                    tmp.groupby("_cliente_base", dropna=False)
                    .agg(Concluídas=("_is_concluido", "sum"), Reagendadas=("_is_reagendar", "sum"), Canceladas=("_is_cancelado", "sum"))
                    .reset_index()
                    .rename(columns={"_cliente_base": "Cliente"})
                )
                df_clientes["Total"] = df_clientes["Concluídas"] + df_clientes["Reagendadas"] + df_clientes["Canceladas"]
                df_clientes = df_clientes.sort_values(["Total", "Cliente"], ascending=[False, True])
                st.dataframe(df_clientes, use_container_width=True, height=360)
        else:
            st.info("Coluna 'Cliente' não encontrada.")
    with cl_right:
        st.subheader("Motivos")
        if reason_col:
            s_motivo = df_f[reason_col].dropna().astype(str).str.strip()
            s_motivo = s_motivo[s_motivo != ""]
            if s_motivo.empty:
                st.info("Sem motivos preenchidos.")
            else:
                bar_chart_counts(s_motivo, top_n=25, y_label="Ocorrências")
        else:
            st.info("Coluna de motivo não encontrada.")

    st.markdown(f"<h2 style='text-align:center;'>{item_label} por Estado</h2>", unsafe_allow_html=True)
    if safe_col(df_f, "UF"):
        s = df_f["UF"].dropna().astype(str).str.strip().str.upper()
        counts = s.value_counts()
        if counts.empty:
            st.info("Sem dados de UF para o gráfico.")
        else:
            df_state = counts.rename_axis("UF").reset_index(name=chart_y)
            fig_state = px.pie(df_state, names="UF", values=chart_y, template="plotly_dark", hole=0.35)
            fig_state.update_layout(height=480, margin=dict(l=20, r=20, t=20, b=20), legend_title_text="UF")
            _c1, _c2, _c3 = st.columns([1, 2, 1])
            with _c2:
                st.plotly_chart(fig_state, use_container_width=True)
    else:
        st.info("Coluna 'UF' não encontrada.")

    st.subheader("Tabela (filtrada)")
    df_show = df_f.copy()
    for internal in ["_data", "_cliente_base"]:
        if internal in df_show.columns:
            df_show = df_show.drop(columns=[internal])
    if show_cols:
        cols_ok = [c for c in show_cols if c in df_show.columns]
        if cols_ok:
            df_show = df_show[cols_ok]
    st.dataframe(df_show, use_container_width=True, height=520)
    download_csv(df_show, filename=cfg["csv_name"])


# =============================
# Dashboard Tiflux
# =============================
def _prepare_tiflux_frame(
    df_raw: pd.DataFrame,
    date_columns: list[str],
    numeric_columns: list[str],
) -> pd.DataFrame:
    df = df_raw.copy()
    df.columns = df.columns.str.strip()
    for column in df.columns:
        if df[column].dtype == object:
            values = df[column].astype("string").str.strip()
            missing = values.eq("") | values.str.upper().eq("NA")
            df[column] = values.mask(missing, pd.NA)
    for column in date_columns:
        if column in df.columns:
            df[column] = to_datetime_series(df[column])
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    for column in ["status", "sla_atendimento", "sla_solucao", "categoria_fonte"]:
        if column in df.columns:
            df[column] = df[column].astype("string").str.strip().str.upper()
    return df


def _tiflux_options(frames: list[pd.DataFrame], column: str) -> list[str]:
    values: list[str] = []
    for frame in frames:
        if column in frame.columns:
            series = frame[column].dropna().astype(str).str.strip()
            values.extend(series[series != ""].tolist())
    return sorted(set(values), key=lambda value: value.casefold())


def _tiflux_apply_selection(
    df: pd.DataFrame,
    column: str,
    selected: list[str],
) -> pd.DataFrame:
    if not selected or column not in df.columns:
        return df
    return df[df[column].astype("string").isin(selected)]


def _tiflux_percent(part: int, total: int) -> Optional[float]:
    return None if total <= 0 else part / total * 100.0


def _tiflux_percent_text(value: Optional[float]) -> str:
    return "Sem base" if value is None else f"{format_number_pt(value, 1)}%"


def _tiflux_minutes_text(minutes: Optional[float]) -> str:
    if minutes is None or pd.isna(minutes):
        return "—"
    seconds = int(round(max(0.0, float(minutes)) * 60))
    hours, remainder = divmod(seconds, 3600)
    mins, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if mins or hours:
        parts.append(f"{mins}min")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _tiflux_sla_summary(df: pd.DataFrame, column: str) -> dict[str, object]:
    values = df[column].astype("string") if column in df.columns else pd.Series(dtype="string")
    yes = int(values.eq("SIM").sum())
    no = int(values.eq("NAO").sum())
    in_progress = int(values.eq("EM_ANDAMENTO").sum())
    denominator = yes + no
    return {
        "sim": yes,
        "nao": no,
        "em_andamento": in_progress,
        "sem_resultado": int(values.isna().sum()),
        "denominador": denominator,
        "taxa": _tiflux_percent(yes, denominator),
    }


def _tiflux_count_chart(
    series: pd.Series,
    *,
    entity_label: str,
    top_n: int = 15,
) -> None:
    values = series.astype("string").str.strip().fillna("Não informado")
    values = values.mask(values.eq(""), "Não informado")
    counts = values.value_counts().head(top_n)
    if counts.empty:
        st.info("Sem dados para o gráfico.")
        return
    chart_data = counts.rename_axis("Categoria").reset_index(name=entity_label)
    fig = px.bar(
        chart_data,
        x="Categoria",
        y=entity_label,
        text=entity_label,
        template="plotly_dark",
        color_discrete_sequence=[COR1],
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(
        height=360,
        margin=dict(l=20, r=20, t=20, b=80),
        xaxis_title="",
        yaxis_title=entity_label,
    )
    fig.update_xaxes(tickangle=-35)
    st.plotly_chart(fig, use_container_width=True)


def _tiflux_sla_chart(df: pd.DataFrame, group_column: str, sla_column: str) -> None:
    if group_column not in df.columns or sla_column not in df.columns:
        st.info("Sem dados suficientes para o gráfico de SLA.")
        return
    data = df[df[sla_column].isin(["SIM", "NAO"])].copy()
    if data.empty:
        st.info("Sem resultados concluídos de SLA para o recorte.")
        return
    data[group_column] = data[group_column].astype("string").str.strip().fillna("Não informado")
    data[group_column] = data[group_column].mask(data[group_column].eq(""), "Não informado")
    grouped = (
        data.assign(_sim=data[sla_column].eq("SIM").astype(int))
        .groupby(group_column, dropna=False)
        .agg(SIM=("_sim", "sum"), Base=(sla_column, "size"))
        .reset_index()
    )
    grouped["Cumprimento"] = grouped["SIM"] / grouped["Base"] * 100
    grouped = grouped.sort_values(["Base", "Cumprimento"], ascending=[False, False]).head(15)
    grouped["Rótulo"] = grouped["Cumprimento"].map(lambda value: f"{format_number_pt(value, 1)}%")
    fig = px.bar(
        grouped,
        x=group_column,
        y="Cumprimento",
        text="Rótulo",
        custom_data=["SIM", "Base"],
        template="plotly_dark",
        color_discrete_sequence=[COR1],
    )
    fig.update_traces(
        textposition="outside",
        cliponaxis=False,
        hovertemplate="%{x}<br>Cumprimento: %{y:.1f}%<br>SIM: %{customdata[0]}<br>Base: %{customdata[1]}<extra></extra>",
    )
    fig.update_layout(
        height=380,
        margin=dict(l=20, r=20, t=20, b=90),
        xaxis_title="",
        yaxis_title="Cumprimento (%)",
    )
    fig.update_xaxes(tickangle=-35)
    fig.update_yaxes(range=[0, 105])
    st.plotly_chart(fig, use_container_width=True)


def _tiflux_flow_chart(tickets: pd.DataFrame) -> None:
    series = []
    for column, label in [("aberto_em", "Aberturas"), ("fechado_em", "Fechamentos")]:
        if column not in tickets.columns:
            continue
        dates = tickets[column].dropna().dt.date
        if dates.empty:
            continue
        counts = dates.value_counts().sort_index()
        series.extend(
            {"Data": day, "Movimento": label, "Tickets": int(quantity)}
            for day, quantity in counts.items()
        )
    if not series:
        st.info("Sem datas válidas para o fluxo de tickets.")
        return
    fig = px.line(
        pd.DataFrame(series),
        x="Data",
        y="Tickets",
        color="Movimento",
        markers=True,
        template="plotly_dark",
        color_discrete_sequence=[COR1, COR2],
    )
    fig.update_layout(height=380, margin=dict(l=20, r=20, t=20, b=40))
    st.plotly_chart(fig, use_container_width=True)


def _tiflux_import_panel() -> None:
    feedback = st.session_state.pop("tiflux_import_feedback", None)
    if feedback:
        st.success(feedback["message"])
        for warning in feedback.get("warnings", []):
            st.warning(warning)

    with st.expander("Importar base TIFLUX (.xlsx)", expanded=False):
        st.caption(
            "O arquivo deve conter as abas Tickets e Chats. Registros existentes são "
            "atualizados por ticket_id e chat_id; IDs novos são acrescentados."
        )
        uploaded_file = st.file_uploader(
            "Selecionar arquivo XLSX",
            type=["xlsx"],
            key="tiflux_xlsx_upload",
        )
        if uploaded_file is None:
            return
        st.caption(f"Arquivo selecionado: {uploaded_file.name}")
        if not st.button(
            "Importar para a planilha base",
            type="primary",
            use_container_width=True,
            key="tiflux_import_button",
        ):
            return

        try:
            with st.spinner("Validando e importando Tickets e Chats..."):
                imported = load_and_validate_tiflux_xlsx(uploaded_file.getvalue())
                result = upsert_tiflux_frames(
                    _get_gspread_client(SCOPES_READWRITE),
                    SPREADSHEET_ID,
                    imported.tickets,
                    imported.chats,
                )
                st.cache_data.clear()
            tickets_result = result[TIFLUX_TICKET_SHEET]
            chats_result = result[TIFLUX_CHAT_SHEET]
            st.session_state["tiflux_import_feedback"] = {
                "message": (
                    "Importação concluída. "
                    f"Tickets: {tickets_result['inseridos']} inseridos, "
                    f"{tickets_result['atualizados']} atualizados e "
                    f"{tickets_result['inalterados']} inalterados. "
                    f"Chats: {chats_result['inseridos']} inseridos, "
                    f"{chats_result['atualizados']} atualizados e "
                    f"{chats_result['inalterados']} inalterados."
                ),
                "warnings": list(imported.warnings),
            }
            st.rerun()
        except Exception as exc:
            st.error(f"Não foi possível importar o arquivo: {exc}")


def render_tiflux_dashboard() -> None:
    st.title("TIFLUX")
    _tiflux_import_panel()

    try:
        tickets_raw = read_sheet(
            SPREADSHEET_ID,
            TIFLUX_TICKET_SHEET,
            fallback_to_first=False,
        )
        chats_raw = read_sheet(
            SPREADSHEET_ID,
            TIFLUX_CHAT_SHEET,
            fallback_to_first=False,
        )
    except Exception as exc:
        st.error(f"Não foi possível ler as abas TIFLUX: {exc}")
        return

    if tickets_raw.empty and chats_raw.empty:
        st.warning(
            "As abas tiflux-ticket e tiflux-chat ainda não possuem dados. "
            "Importe um arquivo XLSX acima."
        )
        return

    tickets = _prepare_tiflux_frame(
        tickets_raw,
        TICKET_DATE_COLUMNS,
        TICKET_NUMERIC_COLUMNS,
    )
    chats = _prepare_tiflux_frame(
        chats_raw,
        CHAT_DATE_COLUMNS,
        CHAT_NUMERIC_COLUMNS,
    )

    all_dates = pd.concat(
        [
            tickets.get("aberto_em", pd.Series(dtype="datetime64[ns]")),
            chats.get("iniciado_em", pd.Series(dtype="datetime64[ns]")),
        ],
        ignore_index=True,
    ).dropna()

    with st.sidebar:
        st.header("Filtros TIFLUX")
        period_option, start_date, end_date = render_period_filter(
            all_dates,
            "tiflux",
            include_all=True,
        )
        ticket_date_label = st.selectbox(
            "Data dos tickets",
            ["Abertura do ticket", "Fechamento do ticket"],
            key="tiflux_ticket_date_type",
        )
        related_chats = st.checkbox(
            "Somente chats dos tickets filtrados",
            value=False,
            key="tiflux_related_chats",
            help="No modo normal, os chats são filtrados pela própria data de início. Neste modo, também precisam estar vinculados aos tickets selecionados.",
        )
        with st.expander("Filtros avançados", expanded=False):
            frames = [tickets, chats]
            sel_cliente = st.multiselect("Cliente", _tiflux_options(frames, "cliente"), key="tiflux_cliente")
            sel_responsavel = st.multiselect("Técnico", _tiflux_options(frames, "responsavel"), key="tiflux_responsavel")
            sel_catalogo = st.multiselect("Catálogo", _tiflux_options(frames, "catalogo"), key="tiflux_catalogo")
            sel_area = st.multiselect("Área", _tiflux_options(frames, "area"), key="tiflux_area")
            sel_item = st.multiselect("Item", _tiflux_options(frames, "item"), key="tiflux_item")
            sel_origem = st.multiselect("Origem", _tiflux_options(frames, "origem"), key="tiflux_origem")
            sel_departamento = st.multiselect("Departamento do chat", _tiflux_options([chats], "departamento"), key="tiflux_departamento")
            sel_natureza = st.multiselect("Natureza do ticket", _tiflux_options([tickets], "natureza"), key="tiflux_natureza")
            sel_causa = st.multiselect("Causa do ticket", _tiflux_options([tickets], "causa"), key="tiflux_causa")
            sel_ticket_status = st.multiselect("Status do ticket", _tiflux_options([tickets], "status"), key="tiflux_ticket_status")
            sel_chat_status = st.multiselect("Status do chat", _tiflux_options([chats], "status"), key="tiflux_chat_status")
            sel_sla_atendimento = st.multiselect("SLA de atendimento", _tiflux_options([tickets], "sla_atendimento"), key="tiflux_sla_atendimento")
            sel_sla_solucao = st.multiselect("SLA de solução", _tiflux_options([tickets], "sla_solucao"), key="tiflux_sla_solucao")

    ticket_date_column = "aberto_em" if ticket_date_label == "Abertura do ticket" else "fechado_em"
    tickets_f = apply_period_filter(tickets, ticket_date_column, period_option, start_date, end_date)
    if ticket_date_column == "fechado_em":
        tickets_f = tickets_f[
            tickets_f["fechado_em"].notna() & tickets_f["status"].eq("FECHADO")
        ]
    chats_f = apply_period_filter(chats, "iniciado_em", period_option, start_date, end_date)

    shared_filters = [
        ("cliente", sel_cliente),
        ("responsavel", sel_responsavel),
        ("catalogo", sel_catalogo),
        ("area", sel_area),
        ("item", sel_item),
        ("origem", sel_origem),
    ]
    for column, selected in shared_filters:
        tickets_f = _tiflux_apply_selection(tickets_f, column, selected)
        chats_f = _tiflux_apply_selection(chats_f, column, selected)
    for column, selected in [
        ("natureza", sel_natureza),
        ("causa", sel_causa),
        ("status", sel_ticket_status),
        ("sla_atendimento", sel_sla_atendimento),
        ("sla_solucao", sel_sla_solucao),
    ]:
        tickets_f = _tiflux_apply_selection(tickets_f, column, selected)
    chats_f = _tiflux_apply_selection(chats_f, "status", sel_chat_status)
    chats_f = _tiflux_apply_selection(chats_f, "departamento", sel_departamento)

    if related_chats:
        selected_ticket_ids = set(tickets_f["ticket_id"].dropna().astype(str))
        chats_f = chats_f[
            chats_f["ticket_id"].notna()
            & chats_f["ticket_id"].astype(str).isin(selected_ticket_ids)
        ]

    total_tickets = int(tickets_f["ticket_id"].nunique()) if "ticket_id" in tickets_f else 0
    tickets_abertos = int(tickets_f["status"].eq("ABERTO").sum()) if "status" in tickets_f else 0
    tickets_fechados = int(tickets_f["status"].eq("FECHADO").sum()) if "status" in tickets_f else 0
    total_chats = int(chats_f["chat_id"].nunique()) if "chat_id" in chats_f else 0
    chats_fechados = int(chats_f["status"].eq("FECHADO").sum()) if "status" in chats_f else 0
    chats_cancelados = int(chats_f["status"].eq("CANCELADO").sum()) if "status" in chats_f else 0

    sla_atendimento = _tiflux_sla_summary(tickets_f, "sla_atendimento")
    sla_solucao = _tiflux_sla_summary(tickets_f, "sla_solucao")
    meta_values = tickets_f.get("meta_sla_percentual", pd.Series(dtype=float)).dropna()
    meta_sla = float(meta_values.mode().iloc[0]) if not meta_values.empty else None

    reference = tickets_f.get("referencia_em", pd.Series(pd.NaT, index=tickets_f.index))
    atendimento_vencido = (
        tickets_f.get("primeiro_atendimento_em", pd.Series(pd.NaT, index=tickets_f.index)).isna()
        & tickets_f.get("vencimento_atendimento_em", pd.Series(pd.NaT, index=tickets_f.index)).notna()
        & (tickets_f.get("vencimento_atendimento_em", pd.Series(pd.NaT, index=tickets_f.index)) < reference)
    )
    solucao_vencida = (
        tickets_f.get("status", pd.Series("", index=tickets_f.index)).eq("ABERTO")
        & tickets_f.get("vencimento_solucao_em", pd.Series(pd.NaT, index=tickets_f.index)).notna()
        & (tickets_f.get("vencimento_solucao_em", pd.Series(pd.NaT, index=tickets_f.index)) < reference)
    )
    estagio_vencido = (
        tickets_f.get("status", pd.Series("", index=tickets_f.index)).eq("ABERTO")
        & tickets_f.get("vencimento_estagio_em", pd.Series(pd.NaT, index=tickets_f.index)).notna()
        & (tickets_f.get("vencimento_estagio_em", pd.Series(pd.NaT, index=tickets_f.index)) < reference)
    )

    closed_attended = chats_f[
        chats_f.get("status", pd.Series("", index=chats_f.index)).eq("FECHADO")
        & chats_f.get("assumido_em", pd.Series(pd.NaT, index=chats_f.index)).notna()
    ]
    closed_unattended = chats_f[
        chats_f.get("status", pd.Series("", index=chats_f.index)).eq("FECHADO")
        & chats_f.get("assumido_em", pd.Series(pd.NaT, index=chats_f.index)).isna()
    ]
    wait_base = closed_attended[closed_attended.get("espera_util_min", pd.Series(dtype=float)).notna()]
    wait_values = wait_base.get("espera_util_min", pd.Series(dtype=float))
    ticket_ratings = tickets_f.get("avaliacao_nota", pd.Series(dtype=float)).dropna()
    chat_ratings = chats_f.get("avaliacao_nota", pd.Series(dtype=float)).dropna()

    overview_tab, tickets_tab, chats_tab, quality_tab, details_tab = st.tabs(
        ["Visão geral", "Tickets", "Chats", "Avaliações e qualidade", "Detalhamento"]
    )

    with overview_tab:
        c1, c2, c3 = st.columns(3)
        with c1:
            kpi_card("Tickets", format_number_pt(float(total_tickets), 0), COR1)
        with c2:
            kpi_card("Tickets em aberto", format_number_pt(float(tickets_abertos), 0), COR2 if tickets_abertos else COR1)
        with c3:
            kpi_card("Tickets fechados", format_number_pt(float(tickets_fechados), 0), COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        c4, c5, c6 = st.columns(3)
        with c4:
            kpi_card("Chats", format_number_pt(float(total_chats), 0), COR1)
        with c5:
            kpi_card("Chats fechados", format_number_pt(float(chats_fechados), 0), COR1)
        with c6:
            cancel_rate = _tiflux_percent(chats_cancelados, total_chats)
            kpi_card(
                f"Chats cancelados ({chats_cancelados})",
                _tiflux_percent_text(cancel_rate),
                COR2 if chats_cancelados else COR1,
            )
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        c7, c8, c9 = st.columns(3)
        with c7:
            rate = sla_atendimento["taxa"]
            color = COR2 if rate is not None and meta_sla is not None and rate < meta_sla else COR1
            kpi_card(
                f"SLA Atendimento ({sla_atendimento['sim']}/{sla_atendimento['denominador']})",
                _tiflux_percent_text(rate),
                color,
            )
        with c8:
            rate = sla_solucao["taxa"]
            color = COR2 if rate is not None and meta_sla is not None and rate < meta_sla else COR1
            kpi_card(
                f"SLA Solução ({sla_solucao['sim']}/{sla_solucao['denominador']})",
                _tiflux_percent_text(rate),
                color,
            )
        with c9:
            kpi_card("Meta de SLA", _tiflux_percent_text(meta_sla), COR1)
        st.caption(
            "As taxas de SLA usam SIM ÷ (SIM + NAO). EM_ANDAMENTO e valores ausentes ficam fora do denominador."
        )
        st.caption(
            f"Atendimento: {sla_atendimento['em_andamento']} em andamento e "
            f"{sla_atendimento['sem_resultado']} sem resultado. Solução: "
            f"{sla_solucao['em_andamento']} em andamento e "
            f"{sla_solucao['sem_resultado']} sem resultado."
        )
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        c10, c11, c12 = st.columns(3)
        with c10:
            kpi_card("Atendimento pendente vencido", str(int(atendimento_vencido.sum())), COR2 if atendimento_vencido.any() else COR1)
        with c11:
            kpi_card("Solução pendente vencida", str(int(solucao_vencida.sum())), COR2 if solucao_vencida.any() else COR1)
        with c12:
            kpi_card("Estágio vencido", str(int(estagio_vencido.sum())), COR2 if estagio_vencido.any() else COR1)

        st.subheader("Entradas e fechamentos de tickets por dia")
        _tiflux_flow_chart(tickets_f)
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Tickets por cliente")
            _tiflux_count_chart(tickets_f.get("cliente", pd.Series(dtype="string")), entity_label="Tickets")
        with g2:
            st.subheader("Chats por cliente")
            _tiflux_count_chart(chats_f.get("cliente", pd.Series(dtype="string")), entity_label="Chats")

    with tickets_tab:
        duration_base = tickets_f[
            tickets_f.get("status", pd.Series("", index=tickets_f.index)).eq("FECHADO")
            & tickets_f.get("duracao_util_min", pd.Series(dtype=float)).notna()
        ].get("duracao_util_min", pd.Series(dtype=float))
        reopened_tickets = int((tickets_f.get("reaberturas", pd.Series(0, index=tickets_f.index)).fillna(0) > 0).sum())
        reopen_sum = int(tickets_f.get("reaberturas", pd.Series(dtype=float)).fillna(0).sum())
        pointed_sum = tickets_f.get("tempo_apontado_min", pd.Series(dtype=float)).dropna().sum()
        t1, t2, t3 = st.columns(3)
        with t1:
            kpi_card("Duração média dos fechados", _tiflux_minutes_text(duration_base.mean() if not duration_base.empty else None), COR1)
        with t2:
            kpi_card("Mediana da duração", _tiflux_minutes_text(duration_base.median() if not duration_base.empty else None), COR1)
        with t3:
            kpi_card("P90 da duração", _tiflux_minutes_text(duration_base.quantile(0.9) if not duration_base.empty else None), COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        t4, t5, t6 = st.columns(3)
        with t4:
            kpi_card("Tickets reabertos", str(reopened_tickets), COR2 if reopened_tickets else COR1)
        with t5:
            kpi_card("Soma de reaberturas", str(reopen_sum), COR2 if reopen_sum else COR1)
        with t6:
            kpi_card("Tempo apontado", _tiflux_minutes_text(float(pointed_sum)), COR1)
        st.caption("Duração útil não representa esforço ativo. Tempo apontado é exibido separadamente.")

        closed_ticket_base = tickets_f[tickets_f.get("status", pd.Series("", index=tickets_f.index)).eq("FECHADO")]
        closed_attendance_sla = _tiflux_sla_summary(closed_ticket_base, "sla_atendimento")
        closed_solution_sla = _tiflux_sla_summary(closed_ticket_base, "sla_solucao")
        closed_sla_table = pd.DataFrame(
            [
                {
                    "Indicador": "SLA de atendimento — somente fechados",
                    "SIM": closed_attendance_sla["sim"],
                    "NAO": closed_attendance_sla["nao"],
                    "Base": closed_attendance_sla["denominador"],
                    "Cumprimento": _tiflux_percent_text(closed_attendance_sla["taxa"]),
                },
                {
                    "Indicador": "SLA de solução — somente fechados",
                    "SIM": closed_solution_sla["sim"],
                    "NAO": closed_solution_sla["nao"],
                    "Base": closed_solution_sla["denominador"],
                    "Cumprimento": _tiflux_percent_text(closed_solution_sla["taxa"]),
                },
            ]
        )
        st.dataframe(closed_sla_table, use_container_width=True, hide_index=True)

        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Tickets por natureza")
            _tiflux_count_chart(tickets_f.get("natureza", pd.Series(dtype="string")), entity_label="Tickets")
        with g2:
            st.subheader("Tickets por causa")
            _tiflux_count_chart(tickets_f.get("causa", pd.Series(dtype="string")), entity_label="Tickets")
        sla_group_label = st.selectbox(
            "Agrupar SLAs por",
            ["Técnico", "Cliente", "Item"],
            key="tiflux_sla_group",
        )
        sla_group_column = {"Técnico": "responsavel", "Cliente": "cliente", "Item": "item"}[sla_group_label]
        g3, g4 = st.columns(2)
        with g3:
            st.subheader(f"SLA de atendimento por {sla_group_label.lower()}")
            _tiflux_sla_chart(tickets_f, sla_group_column, "sla_atendimento")
        with g4:
            st.subheader(f"SLA de solução por {sla_group_label.lower()}")
            _tiflux_sla_chart(tickets_f, sla_group_column, "sla_solucao")

        ticket_detail_option = st.selectbox(
            "Detalhar indicador de tickets",
            [
                "Todos os tickets filtrados",
                "Tickets em aberto",
                "SLA de atendimento não cumprido",
                "SLA de solução não cumprido",
                "Atendimento pendente vencido",
                "Solução pendente vencida",
                "Estágio vencido",
            ],
            key="tiflux_ticket_detail",
        )
        ticket_detail = tickets_f.copy()
        if ticket_detail_option == "Tickets em aberto":
            ticket_detail = ticket_detail[ticket_detail["status"].eq("ABERTO")]
        elif ticket_detail_option == "SLA de atendimento não cumprido":
            ticket_detail = ticket_detail[ticket_detail["sla_atendimento"].eq("NAO")]
        elif ticket_detail_option == "SLA de solução não cumprido":
            ticket_detail = ticket_detail[ticket_detail["sla_solucao"].eq("NAO")]
        elif ticket_detail_option == "Atendimento pendente vencido":
            ticket_detail = ticket_detail[atendimento_vencido]
        elif ticket_detail_option == "Solução pendente vencida":
            ticket_detail = ticket_detail[solucao_vencida]
        elif ticket_detail_option == "Estágio vencido":
            ticket_detail = ticket_detail[estagio_vencido]
        ticket_detail_columns = [
            column for column in [
                "ticket_id", "cliente", "responsavel", "status", "aberto_em", "fechado_em",
                "sla_atendimento", "sla_solucao", "catalogo", "area", "item", "natureza", "causa",
            ] if column in ticket_detail.columns
        ]
        st.dataframe(ticket_detail[ticket_detail_columns], use_container_width=True, height=340)

    with chats_tab:
        cancel_rate = _tiflux_percent(chats_cancelados, total_chats)
        wait_mean = wait_values.mean() if not wait_values.empty else None
        wait_median = wait_values.median() if not wait_values.empty else None
        wait_p90 = wait_values.quantile(0.9) if not wait_values.empty else None
        above_10 = int((wait_values > 10).sum()) if not wait_values.empty else 0
        above_30 = int((wait_values > 30).sum()) if not wait_values.empty else 0
        above_60 = int((wait_values > 60).sum()) if not wait_values.empty else 0
        cancelled_chats = chats_f[chats_f.get("status", pd.Series("", index=chats_f.index)).eq("CANCELADO")]
        cancelled_assumed = cancelled_chats[
            cancelled_chats.get("assumido_em", pd.Series(pd.NaT, index=cancelled_chats.index)).notna()
        ]
        cancelled_unassumed = cancelled_chats[
            cancelled_chats.get("assumido_em", pd.Series(pd.NaT, index=cancelled_chats.index)).isna()
        ]
        h1, h2, h3 = st.columns(3)
        with h1:
            kpi_card("Fechados com atendimento", str(len(closed_attended)), COR1)
        with h2:
            kpi_card("Fechados sem atendimento", str(len(closed_unattended)), COR2 if len(closed_unattended) else COR1)
        with h3:
            kpi_card(f"Cancelados ({chats_cancelados})", _tiflux_percent_text(cancel_rate), COR2 if chats_cancelados else COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        h4, h5, h6 = st.columns(3)
        with h4:
            kpi_card("Espera média", _tiflux_minutes_text(wait_mean), COR1)
        with h5:
            kpi_card("Mediana da espera", _tiflux_minutes_text(wait_median), COR1)
        with h6:
            kpi_card("P90 da espera", _tiflux_minutes_text(wait_p90), COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        h7, h8, h9 = st.columns(3)
        with h7:
            kpi_card("Espera acima de 10 min", f"{above_10} ({_tiflux_percent_text(_tiflux_percent(above_10, len(wait_values)))})", COR2 if above_10 else COR1)
        with h8:
            kpi_card("Espera acima de 30 min", f"{above_30} ({_tiflux_percent_text(_tiflux_percent(above_30, len(wait_values)))})", COR2 if above_30 else COR1)
        with h9:
            kpi_card("Espera acima de 60 min", f"{above_60} ({_tiflux_percent_text(_tiflux_percent(above_60, len(wait_values)))})", COR2 if above_60 else COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        h10, h11, h12 = st.columns(3)
        with h10:
            kpi_card("Cancelados após assumir", str(len(cancelled_assumed)), COR2 if len(cancelled_assumed) else COR1)
        with h11:
            kpi_card("Cancelados sem assumir", str(len(cancelled_unassumed)), COR2 if len(cancelled_unassumed) else COR1)
        with h12:
            phone_counts = chats_f.get("telefone_id", pd.Series(dtype="string")).dropna().value_counts()
            unique_contacts = int(len(phone_counts))
            kpi_card("Contatos únicos", str(unique_contacts), COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        h13, h14, h15 = st.columns(3)
        with h13:
            repeated_contacts = int((phone_counts > 1).sum())
            kpi_card("Contatos repetidos", str(repeated_contacts), COR2 if repeated_contacts else COR1)
        with h14:
            duration_values = closed_attended.get("duracao_util_min", pd.Series(dtype=float)).dropna()
            kpi_card("Duração média dos chats", _tiflux_minutes_text(duration_values.mean() if not duration_values.empty else None), COR1)
        with h15:
            kpi_card("Soma do tempo dos chats", _tiflux_minutes_text(duration_values.sum() if not duration_values.empty else None), COR1)
        st.caption("As faixas de espera são análises operacionais e não representam SLAs definidos para chats.")
        st.caption("A soma das durações não representa horas produtivas; conversas simultâneas podem se sobrepor.")

        classification_original = int(chats_f.get("categoria_fonte", pd.Series(dtype="string")).eq("CHAT").sum())
        classification_linked = int(chats_f.get("categoria_fonte", pd.Series(dtype="string")).eq("TICKET_VINCULADO").sum())
        classification_available = int(chats_f.get("item", pd.Series(dtype="string")).notna().sum())
        h16, h17, h18 = st.columns(3)
        with h16:
            kpi_card("Classificação original do chat", str(classification_original), COR1)
        with h17:
            kpi_card("Classificação pelo ticket vinculado", str(classification_linked), COR1)
        with h18:
            kpi_card("Chats com classificação disponível", str(classification_available), COR1)

        st.subheader("Chats iniciados por dia")
        line_chart_by_day(chats_f.get("iniciado_em", pd.Series(dtype="datetime64[ns]")), "Chats")
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Chats por técnico")
            _tiflux_count_chart(chats_f.get("responsavel", pd.Series(dtype="string")), entity_label="Chats")
        with g2:
            st.subheader("Chats por item")
            _tiflux_count_chart(chats_f.get("item", pd.Series(dtype="string")), entity_label="Chats")

        if "iniciado_em" in chats_f.columns and chats_f["iniciado_em"].notna().any():
            time_data = chats_f[chats_f["iniciado_em"].notna()].copy()
            weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
            time_data["Dia da semana"] = time_data["iniciado_em"].dt.dayofweek.map(dict(enumerate(weekdays)))
            time_data["Hora"] = time_data["iniciado_em"].dt.hour
            g3, g4 = st.columns(2)
            with g3:
                st.subheader("Chats por dia da semana")
                weekday_counts = time_data["Dia da semana"].value_counts().reindex(weekdays, fill_value=0).reset_index()
                weekday_counts.columns = ["Dia da semana", "Chats"]
                fig = px.bar(weekday_counts, x="Dia da semana", y="Chats", text="Chats", template="plotly_dark", color_discrete_sequence=[COR1])
                fig.update_traces(textposition="outside", cliponaxis=False)
                fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=60), xaxis_title="")
                st.plotly_chart(fig, use_container_width=True)
            with g4:
                st.subheader("Chats por hora de entrada")
                hour_counts = time_data["Hora"].value_counts().sort_index().reset_index()
                hour_counts.columns = ["Hora", "Chats"]
                fig = px.bar(hour_counts, x="Hora", y="Chats", text="Chats", template="plotly_dark", color_discrete_sequence=[COR1])
                fig.update_traces(textposition="outside", cliponaxis=False)
                fig.update_layout(height=360, margin=dict(l=20, r=20, t=20, b=40), xaxis_title="Hora")
                fig.update_xaxes(dtick=1)
                st.plotly_chart(fig, use_container_width=True)

        chat_detail_option = st.selectbox(
            "Detalhar indicador de chats",
            [
                "Todos os chats filtrados",
                "Fechados com atendimento",
                "Fechados sem atendimento",
                "Cancelados",
                "Cancelados após assumir",
                "Cancelados sem assumir",
                "Espera acima de 10 min",
                "Espera acima de 30 min",
                "Espera acima de 60 min",
            ],
            key="tiflux_chat_detail",
        )
        chat_detail = chats_f.copy()
        if chat_detail_option == "Fechados com atendimento":
            chat_detail = closed_attended
        elif chat_detail_option == "Fechados sem atendimento":
            chat_detail = closed_unattended
        elif chat_detail_option == "Cancelados":
            chat_detail = chat_detail[chat_detail["status"].eq("CANCELADO")]
        elif chat_detail_option == "Cancelados após assumir":
            chat_detail = cancelled_assumed
        elif chat_detail_option == "Cancelados sem assumir":
            chat_detail = cancelled_unassumed
        elif chat_detail_option == "Espera acima de 10 min":
            chat_detail = wait_base[wait_base["espera_util_min"] > 10]
        elif chat_detail_option == "Espera acima de 30 min":
            chat_detail = wait_base[wait_base["espera_util_min"] > 30]
        elif chat_detail_option == "Espera acima de 60 min":
            chat_detail = wait_base[wait_base["espera_util_min"] > 60]
        chat_detail_columns = [
            column for column in [
                "chat_id", "ticket_id", "cliente", "responsavel", "status", "iniciado_em",
                "assumido_em", "finalizado_em", "espera_util_min", "duracao_util_min", "catalogo", "area", "item",
            ] if column in chat_detail.columns
        ]
        st.dataframe(chat_detail[chat_detail_columns], use_container_width=True, height=340)

    with quality_tab:
        ticket_rating_rate = _tiflux_percent(len(ticket_ratings), tickets_fechados)
        chat_rating_rate = _tiflux_percent(len(chat_ratings), chats_fechados)
        positive_chat = int(chat_ratings.isin([4, 5]).sum())
        q1, q2, q3 = st.columns(3)
        with q1:
            ticket_mean = ticket_ratings.mean() if not ticket_ratings.empty else None
            kpi_card(f"Avaliações de tickets ({len(ticket_ratings)})", f"{format_number_pt(ticket_mean, 2)}/5" if ticket_mean is not None else "Sem base", COR1)
        with q2:
            chat_mean = chat_ratings.mean() if not chat_ratings.empty else None
            kpi_card(f"Avaliações de chats ({len(chat_ratings)})", f"{format_number_pt(chat_mean, 2)}/5" if chat_mean is not None else "Sem base", COR1)
        with q3:
            positive_rate = _tiflux_percent(positive_chat, len(chat_ratings))
            kpi_card(f"Avaliações positivas de chat ({positive_chat}/{len(chat_ratings)})", _tiflux_percent_text(positive_rate), COR1)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        q4, q5 = st.columns(2)
        with q4:
            kpi_card(f"Participação nas avaliações de tickets ({len(ticket_ratings)}/{tickets_fechados})", _tiflux_percent_text(ticket_rating_rate), COR1)
        with q5:
            kpi_card(f"Participação nas avaliações de chats ({len(chat_ratings)}/{chats_fechados})", _tiflux_percent_text(chat_rating_rate), COR1)

        g1, g2 = st.columns(2)
        with g1:
            st.subheader("Distribuição das notas de tickets")
            if ticket_ratings.empty:
                st.info("Sem avaliações de tickets no recorte.")
            else:
                _tiflux_count_chart(ticket_ratings, entity_label="Respostas", top_n=5)
        with g2:
            st.subheader("Distribuição das notas de chats")
            if chat_ratings.empty:
                st.info("Sem avaliações de chats no recorte.")
            else:
                _tiflux_count_chart(chat_ratings, entity_label="Respostas", top_n=5)

        quality_rows = []
        for entity, frame, fields in [
            ("Tickets", tickets_f, ["responsavel", "item", "natureza", "causa"]),
            ("Chats", chats_f, ["cliente", "responsavel", "item"]),
        ]:
            for field in fields:
                if field not in frame.columns:
                    continue
                missing = int(frame[field].isna().sum())
                quality_rows.append(
                    {
                        "Entidade": entity,
                        "Campo": field,
                        "Não informado": missing,
                        "Percentual": _tiflux_percent(missing, len(frame)),
                    }
                )
        st.subheader("Qualidade de preenchimento")
        quality_df = pd.DataFrame(quality_rows)
        if not quality_df.empty:
            quality_df["Percentual"] = quality_df["Percentual"].map(_tiflux_percent_text)
            st.dataframe(quality_df, use_container_width=True, hide_index=True)

        low_ratings = chats_f[chats_f.get("avaliacao_nota", pd.Series(dtype=float)).isin([1, 2, 3])]
        st.subheader("Chats com notas de 1 a 3")
        low_rating_columns = [
            column for column in [
                "chat_id", "ticket_id", "cliente", "responsavel", "avaliacao_nota",
                "avaliacao_comentario", "espera_util_min", "iniciado_em",
            ] if column in low_ratings.columns
        ]
        if low_ratings.empty:
            st.info("Nenhum chat com nota de 1 a 3 no recorte.")
        else:
            st.dataframe(low_ratings[low_rating_columns], use_container_width=True, height=300)

    with details_tab:
        entity = st.radio("Entidade", ["Tickets", "Chats"], horizontal=True, key="tiflux_detail_entity")
        detail_source = tickets_f if entity == "Tickets" else chats_f
        default_columns = (
            ["ticket_id", "cliente", "responsavel", "status", "aberto_em", "sla_atendimento", "sla_solucao", "item"]
            if entity == "Tickets"
            else ["chat_id", "ticket_id", "cliente", "responsavel", "status", "iniciado_em", "espera_util_min", "item"]
        )
        available_columns = list(detail_source.columns)
        selected_columns = st.multiselect(
            "Colunas da tabela",
            options=available_columns,
            default=[column for column in default_columns if column in available_columns],
            key=f"tiflux_detail_columns_{entity.lower()}",
        )
        detail_view = detail_source[selected_columns] if selected_columns else detail_source
        st.dataframe(detail_view, use_container_width=True, height=520)
        download_csv(
            detail_view,
            filename=f"tiflux_{entity.lower()}_filtrado.csv",
        )


# =============================
# Render final
# =============================
mode = st.session_state.view_mode
if mode == "GERAL":
    render_general_dashboard()
elif mode == "TIFLUX":
    render_tiflux_dashboard()
elif mode == "CADASTRAR":
    render_register_page()
elif mode in MODULES:
    render_dashboard(mode)
else:
    st.session_state.view_mode = "GERAL"
    st.rerun()
