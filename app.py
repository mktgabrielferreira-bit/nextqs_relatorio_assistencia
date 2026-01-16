import re
from datetime import date
from typing import Optional

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
from google.oauth2.service_account import Credentials


# =============================
# Config
# =============================
st.set_page_config(page_title="📊 Relatório de Instalações NextQS", layout="wide")



def require_password() -> None:
    """Bloqueia o app por senha (lida dos secrets).
    Aceita as chaves:
      - app_password (novo padrão)
      - SENHA_DASH   (compatibilidade com apps antigos)
    """
    app_pwd = st.secrets.get("app_password") or st.secrets.get("SENHA_DASH")
    if not app_pwd:
        return  # sem senha configurada -> acesso livre

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    # Tela de login (centralizada e com input menor)
    st.markdown(
        """
        <div style="text-align:center; padding: 56px 0 8px 0;">
            <h1 style="font-size: 44px; margin-bottom: 8px;">🔒 Acesso restrito</h1>
            <p style="opacity:0.75; font-size: 18px; margin: 0;">
                Digite a senha para acessar o relatório
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, mid, right = st.columns([2.2, 2.6, 2.2])
    with mid:
        senha = st.text_input("Senha de acesso", type="password", label_visibility="visible")
        if st.button("Entrar", use_container_width=True):
            if senha == app_pwd:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Senha incorreta.")

    st.stop()


# Chame isso antes de QUALQUER coisa do relatório
require_password()


SCOPES_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Ajuste aqui se os nomes das colunas na planilha forem diferentes
COL_DATA = "Data"
COL_HORA_INICIO = "Início"
COL_HORA_FIM = "Término"
COL_DURACAO = "Duração"  # usado no "Tempo Médio"
COL_MODALIDADE = "Modalidade"
COL_TECNICO = "Técnico"  # ranking de técnicos (fallbacks abaixo)
COL_CONSULTOR = "Consultor"
COL_CLIENTE = "Cliente"
COL_UF = "UF"
COL_STATUS = "Status"
COL_CIDADE = "Cidade"
COL_QT_QUIOSQUE = "Quantidade Quiosque"
COL_QT_PLAYERS = "Quantidade Players"




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
    s = s.astype(str).str.strip()
    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def sum_numeric(df: pd.DataFrame, col: str) -> int:
    if col not in df.columns:
        return 0
    return int(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())


def download_csv(df: pd.DataFrame, filename: str = "relatorio_filtrado.csv") -> None:
    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Baixar CSV filtrado",
        data=csv,
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
    )


def _parse_duration_to_minutes(value) -> Optional[float]:
    """
    Tenta converter a coluna 'Duração' para minutos.

    Aceita:
      - número (assume minutos)
      - "HH:MM" / "HH:MM:SS"
      - textos pt-br: "50 minutos", "1 hora", "2 horas e 30 minutos", etc.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    # Número -> minutos
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)

    s = str(value).strip().lower()
    if not s:
        return None

    # "HH:MM" / "HH:MM:SS"
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        parts = [int(x) for x in s.split(":")]
        if len(parts) == 2:
            h, m = parts
            return h * 60 + m
        if len(parts) == 3:
            h, m, sec = parts
            return h * 60 + m + sec / 60.0

    # "2 horas e 30 minutos" / "1h 20m" / "90 min" etc.
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

    # fallback: extrai primeiro número e assume minutos
    mn = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if mn:
        return float(mn.group(1).replace(",", "."))
    return None


def format_minutes_pt(minutes: Optional[float]) -> str:
    """Formato compacto para caber no KPI (ex.: 2h e 9 min)."""
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
    if s.empty:
        return "—"
    return s.value_counts().index[0]


def kpi_card(label: str, value: str) -> None:
    st.markdown(
        f"""
        <div style="
            padding: 10px 12px;
            border-radius: 10px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            ">
            <div style="font-size: 14px; opacity: 0.85;">{label}</div>
            <div style="font-size: 34px; font-weight: 800; color: #2ecc71; line-height: 1.1;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def bar_chart_counts(series: pd.Series, top_n: int = 10, y_label: str = "Instalações") -> None:
    """Bar chart (Plotly, tema escuro) para contagens de uma série categórica."""
    if series is None:
        st.info("Sem dados para o gráfico.")
        return

    s = series.dropna().astype(str).str.strip()
    counts = s.value_counts().head(top_n)
    if counts.empty:
        st.info("Sem dados para o gráfico.")
        return

    dfc = counts.rename_axis("Categoria").reset_index(name=y_label)
    fig = px.bar(
        dfc,
        x="Categoria",
        y=y_label,
        text=y_label,
        template="plotly_dark",
        color_discrete_sequence=["#7FB3FF"],
    )
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.update_layout(
        height=360,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="",
        yaxis_title=y_label,
    )
    fig.update_xaxes(tickangle=-35)
    st.plotly_chart(fig, use_container_width=True)


def line_chart_by_day(dates: pd.Series, y_label: str = "Instalações") -> None:
    """Line chart (Plotly, tema escuro) agregando por dia."""
    if dates is None:
        st.info("Sem dados para o gráfico.")
        return
    s = pd.to_datetime(dates, errors="coerce").dropna()
    if s.empty:
        st.info("Sem dados para o gráfico.")
        return

    counts = s.dt.date.value_counts().sort_index()
    dfd = pd.DataFrame({"Data": list(counts.index), y_label: list(counts.values)})

    fig = px.line(
        dfd,
        x="Data",
        y=y_label,
        markers=True,
        template="plotly_dark",
    )
    fig.update_layout(
        height=360,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Data",
        yaxis_title=y_label,
    )
    st.plotly_chart(fig, use_container_width=True)


def histogram_by_hour(time_series: pd.Series, y_label: str = "Instalações") -> None:
    """Histograma por hora (Plotly, tema escuro)."""
    if time_series is None:
        st.info("Sem dados para o gráfico.")
        return
    s = time_series.dropna().astype(str).str.strip()
    if s.empty:
        st.info("Sem dados para o gráfico.")
        return

    def _to_hour(x: str) -> Optional[int]:
        x = x.strip()
        if not x:
            return None
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", x)
        if m:
            h = int(m.group(1))
            return h if 0 <= h <= 23 else None
        try:
            dt = pd.to_datetime(x, errors="coerce")
            if pd.isna(dt):
                return None
            return int(dt.hour)
        except Exception:
            return None

    hours = s.map(_to_hour).dropna().astype(int)
    if hours.empty:
        st.info("Sem horários válidos para o gráfico.")
        return

    counts = hours.value_counts().sort_index()
    dfh = pd.DataFrame({"Hora": list(counts.index), y_label: list(counts.values)})

    fig = px.bar(
        dfh,
        x="Hora",
        y=y_label,
        template="plotly_dark",
        color_discrete_sequence=["#7FB3FF"],
    )
    fig.update_layout(
        height=360,
        margin=dict(l=50, r=20, t=20, b=60),
        xaxis_title="",
        yaxis_title=y_label,
    )
    fig.update_xaxes(dtick=1)
    st.plotly_chart(fig, use_container_width=True)


def brazil_state_pin_map(uf_series: pd.Series) -> None:
    """Mapa do Brasil com pinos (ScatterGeo) por UF, mostrando quantidade."""
    if uf_series is None:
        st.info("Sem dados para o mapa.")
        return

    # Centróides aproximados por UF
    uf_coords = {
        "AC": (-8.77, -70.55), "AL": (-9.62, -36.82), "AP": (1.41, -51.77), "AM": (-3.47, -65.10),
        "BA": (-12.97, -38.50), "CE": (-3.73, -38.52), "DF": (-15.79, -47.88), "ES": (-20.32, -40.34),
        "GO": (-16.68, -49.25), "MA": (-2.53, -44.30), "MT": (-15.60, -56.10), "MS": (-20.45, -54.62),
        "MG": (-19.92, -43.94), "PA": (-1.45, -48.50), "PB": (-7.12, -34.86), "PR": (-25.43, -49.27),
        "PE": (-8.05, -34.90), "PI": (-5.09, -42.80), "RJ": (-22.91, -43.17), "RN": (-5.79, -35.21),
        "RS": (-30.03, -51.23), "RO": (-8.76, -63.90), "RR": (2.82, -60.67), "SC": (-27.59, -48.55),
        "SP": (-23.55, -46.63), "SE": (-10.91, -37.07), "TO": (-10.25, -48.33),
    }

    s = uf_series.dropna().astype(str).str.strip().str.upper()
    if s.empty:
        st.info("Sem dados para o mapa.")
        return

    counts = s.value_counts()
    rows = []
    for uf, qtd in counts.items():
        if uf in uf_coords:
            lat, lon = uf_coords[uf]
            rows.append({"UF": uf, "Instalações": int(qtd), "lat": lat, "lon": lon})

    if not rows:
        st.info("Sem UFs válidas para o mapa.")
        return

    dfm = pd.DataFrame(rows)
    dfm["size"] = (dfm["Instalações"] ** 0.8) * 6 + 10

    fig = px.scatter_geo(
        dfm,
        lat="lat",
        lon="lon",
        size="size",
        hover_name="UF",
        hover_data={"Instalações": True, "lat": False, "lon": False, "size": False},
        text="Instalações",
        template="plotly_dark",
    )

    fig.update_traces(textposition="top center")
    fig.update_layout(
        height=520,
        margin=dict(l=20, r=20, t=20, b=20),
    )
    fig.update_geos(
        scope="south america",
        projection_type="mercator",
        center=dict(lat=-14.2, lon=-51.9),
        lataxis_range=[-34, 6],
        lonaxis_range=[-75, -32],
        showland=True,
        landcolor="rgb(20, 24, 28)",
        showcountries=True,
        countrycolor="rgba(255,255,255,0.15)",
        showocean=True,
        oceancolor="rgb(10, 12, 14)",
        coastlinecolor="rgba(255,255,255,0.15)",
    )

    st.plotly_chart(fig, use_container_width=True)


def get_reagendamento_rate(df: pd.DataFrame) -> Optional[float]:
    """Taxa de reagendamentos (heurística)."""
    col = first_existing_col(
        df,
        [
            "Reagendamento",
            "Reagendado",
            "Reagendamentos",
            "Reagendar",
            "Status",
            "Motivo",
            "Observação",
            "Observacao",
        ],
    )
    if not col:
        return None

    s = df[col].astype(str).str.lower()
    is_reag = s.str.contains("reagend", na=False) | s.isin({"sim", "yes", "true", "1"})
    if len(s) == 0:
        return None
    return float(is_reag.mean())


def read_sheet(spreadsheet_id: str, sheet_name: Optional[str]) -> pd.DataFrame:
    """
    Lê dados do Google Sheets via Service Account (st.secrets["google_service_account"]).
    Observação: SEM CACHE para garantir sincronismo a cada mudança de filtro (Streamlit rerun).
    """
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"],
        scopes=SCOPES_READONLY,
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
    except Exception:
        ws = sh.sheet1

    records = ws.get_all_records()
    return pd.DataFrame(records)


# =============================
# Header + Load
# =============================
st.title("📊 Relatório de Instalações NextQS")

SPREADSHEET_ID = st.secrets.get("spreadsheet_id", "")
SHEET_NAME_DEFAULT = st.secrets.get("sheet_name", "2026")

if not SPREADSHEET_ID:
    st.error("Faltou configurar `spreadsheet_id` nos secrets.")
    st.stop()

with st.sidebar:
    st.header("Filtros")
    sheet_name = st.text_input(
        "Nome da aba (worksheet)",
        value=SHEET_NAME_DEFAULT,
        help="Ex.: 2026. Se estiver vazio/errado, o app usa a primeira aba.",
    ).strip()

# Sempre relê a planilha (sincronismo a cada alteração de filtro)
df_raw = read_sheet(SPREADSHEET_ID, sheet_name)

if df_raw.empty:
    st.warning("A planilha não retornou dados.")
    st.stop()

df = df_raw.copy()

# Normalização de data
df["_data"] = to_date_series(df[COL_DATA]) if safe_col(df, COL_DATA) else pd.NaT
has_valid_dates = df["_data"].notna().any()
ref_dt = df["_data"].dropna().max() if has_valid_dates else pd.Timestamp.today()

ref_year = int(ref_dt.year)
ref_month = int(ref_dt.month)

# =============================
# Sidebar: filtros principais (radio)
# =============================
with st.sidebar:
    period_option = st.radio(
        "Período",
        options=["Este mês", "Este ano"],
        index=0,  # padrão: Este mês
        label_visibility="visible",
    )

    # filtros avançados (mantidos)
    with st.expander("Filtros avançados", expanded=False):

        def multiselect_filter(label: str, col: str) -> list[str]:
            if not safe_col(df, col):
                st.caption(f"Coluna ausente: {col}")
                return []
            vals = sorted(df[col].dropna().astype(str).unique().tolist())
            return st.multiselect(label, options=vals)

        sel_modalidade = multiselect_filter("Modalidade", COL_MODALIDADE)
        sel_uf = multiselect_filter("UF", COL_UF)
        sel_cidade = multiselect_filter("Cidade", COL_CIDADE)
        sel_cliente = multiselect_filter("Cliente", COL_CLIENTE)

        tecnico_col = first_existing_col(df, [COL_TECNICO, "Tecnico", "Técnicos", "Tecnicos"])
        consultor_col = first_existing_col(df, [COL_CONSULTOR, "Consultores", "Consultor(a)"])

        if tecnico_col:
            sel_tecnico = multiselect_filter("Técnico", tecnico_col)
        else:
            sel_tecnico = []
            st.caption("Coluna de Técnico não encontrada.")

        if consultor_col and consultor_col != tecnico_col:
            sel_consultor = multiselect_filter("Consultor", consultor_col)
        else:
            sel_consultor = []

        st.divider()
        show_cols = st.multiselect(
            "Colunas na tabela",
            options=[c for c in df.columns if not c.startswith("_")],
            default=[c for c in df.columns if not c.startswith("_")],
        )

# =============================
# Apply filters
# =============================
df_f = df.copy()

if has_valid_dates:
    if period_option == "Este mês":
        start_date = date(ref_year, ref_month, 1)
        end_date = ref_dt.date()
    else:  # Este ano
        start_date = date(ref_year, 1, 1)
        end_date = ref_dt.date()

    df_f = df_f[(df_f["_data"].dt.date >= start_date) & (df_f["_data"].dt.date <= end_date)]


def apply_multiselect(df_in: pd.DataFrame, col: str, selected: list[str]) -> pd.DataFrame:
    if not selected or col not in df_in.columns:
        return df_in
    return df_in[df_in[col].astype(str).isin(selected)]


df_f = apply_multiselect(df_f, COL_MODALIDADE, sel_modalidade if "sel_modalidade" in locals() else [])
df_f = apply_multiselect(df_f, COL_UF, sel_uf if "sel_uf" in locals() else [])
df_f = apply_multiselect(df_f, COL_CIDADE, sel_cidade if "sel_cidade" in locals() else [])
df_f = apply_multiselect(df_f, COL_CLIENTE, sel_cliente if "sel_cliente" in locals() else [])

if "tecnico_col" in locals() and tecnico_col:
    df_f = apply_multiselect(df_f, tecnico_col, sel_tecnico if "sel_tecnico" in locals() else [])
if "consultor_col" in locals() and consultor_col:
    df_f = apply_multiselect(df_f, consultor_col, sel_consultor if "sel_consultor" in locals() else [])

# =============================
# KPIs (destaques)
# =============================
total_instalacoes = len(df_f)

tempo_medio_str = "—"
if safe_col(df_f, COL_DURACAO):
    mins = df_f[COL_DURACAO].map(_parse_duration_to_minutes).dropna()
    tempo_medio_str = format_minutes_pt(mins.mean()) if not mins.empty else "—"

modalidade_mais_comum = mode_value(df_f[COL_MODALIDADE]) if safe_col(df_f, COL_MODALIDADE) else "—"

reag_rate = get_reagendamento_rate(df_f)
taxa_reag = f"{reag_rate*100:.1f}%" if reag_rate is not None else "—"

k1, k2, k3, k4 = st.columns(4)
with k1:
    kpi_card("Total de Instalações", f"{total_instalacoes}")
with k2:
    kpi_card("Tempo Médio", tempo_medio_str)
with k3:
    kpi_card("Modalidade mais comum", modalidade_mais_comum)
with k4:
    kpi_card("Taxa de Reagendamentos", taxa_reag)

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# =============================
# Gráfico: Instalações por dia
# =============================
st.subheader("Instalações por dia")
if has_valid_dates and df_f["_data"].notna().any():
    line_chart_by_day(df_f["_data"])
else:
    st.info("Sem coluna de data válida para série temporal (Data agendada).")

# =============================
# Se tiver mais de 1 mês: meses com mais instalações
# =============================
if has_valid_dates and df_f["_data"].notna().any():
    months = df_f["_data"].dt.to_period("M").dropna()
    if months.nunique() > 1:
        st.subheader("Meses com mais Instalações")
        counts = months.astype(str).value_counts().sort_values(ascending=False)
        dfm = counts.rename_axis("Mês").reset_index(name="Instalações")

        fig = px.bar(
            dfm,
            x="Mês",
            y="Instalações",
            text="Instalações",
            template="plotly_dark",
            color_discrete_sequence=["#7FB3FF"],
        )
        fig.update_traces(textposition="outside", cliponaxis=False)
        fig.update_layout(
            height=360,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title="",
            yaxis_title="Instalações",
        )
        fig.update_xaxes(tickangle=-35)
        st.plotly_chart(fig, use_container_width=True)

# =============================
# Gráficos: Modalidade e Status (alinhados)
# =============================
g1, g2 = st.columns(2)
with g1:
    st.subheader("Instalações por Modalidade")
    if safe_col(df_f, COL_MODALIDADE):
        bar_chart_counts(df_f[COL_MODALIDADE], top_n=20, y_label="Instalações")
    else:
        st.info("Coluna 'Modalidade' não encontrada.")

with g2:
    st.subheader("Status das Instalações")
    status_col = first_existing_col(
        df_f,
        [
            COL_STATUS,
            "Status da Instalação",
            "Status Instalação",
            "Situacao",
            "Situação",
        ],
    )
    if status_col:
        bar_chart_counts(df_f[status_col], top_n=20, y_label="Instalações")
    else:
        st.info("Coluna de status não encontrada (ex.: 'Status').")

# =============================
# Instalações por Técnico + Horário das Instalações (lado a lado, gráficos)
# =============================
c_left, c_right = st.columns(2)

with c_left:
    st.subheader("Instalações por Técnico")
    tecnico_col_rank = first_existing_col(df_f, [COL_TECNICO, "Tecnico", "Técnicos", "Tecnicos"])
    if tecnico_col_rank:
        tech_counts = (
            df_f[tecnico_col_rank]
            .dropna()
            .astype(str)
            .str.strip()
            .value_counts()
            .reset_index()
        )
        tech_counts.columns = ["Técnico", "Instalações"]

        fig_tec = px.bar(
            tech_counts,
            x="Técnico",
            y="Instalações",
            text="Instalações",
            template="plotly_dark",
            color_discrete_sequence=["#7FB3FF"],
        )
        fig_tec.update_traces(textposition="outside", cliponaxis=False)
        fig_tec.update_layout(
            height=360,
            margin=dict(l=50, r=20, t=20, b=60),
            xaxis_title="",
            yaxis_title="Instalações",
        )
        fig_tec.update_xaxes(tickangle=-35)
        st.plotly_chart(fig_tec, use_container_width=True)
    else:
        st.info("Coluna de Técnico não encontrada.")

with c_right:
    st.subheader("Horário das Instalações")
    hora_col = first_existing_col(df_f, [COL_HORA_INICIO, "Hora início", "Hora Inicio", "Início", "Inicio"])
    if hora_col:
        histogram_by_hour(df_f[hora_col], y_label="Instalações")
    else:
        st.info("Coluna de horário inicial não encontrada (ex.: 'Hora inicio').")

# =============================
# Instalações por Estado (Pizza)
# =============================
st.markdown("<h2 style='text-align:center;'>Instalações por Estado</h2>", unsafe_allow_html=True)

if safe_col(df_f, COL_UF):
    s = df_f[COL_UF].dropna().astype(str).str.strip().str.upper()
    counts = s.value_counts()
    if counts.empty:
        st.info("Sem dados de UF para o gráfico.")
    else:
        df_state = counts.rename_axis("UF").reset_index(name="Instalações")

        fig_state = px.pie(
            df_state,
            names="UF",
            values="Instalações",
            template="plotly_dark",
            hole=0.35,
        )
        fig_state.update_layout(
            height=480,
            margin=dict(l=20, r=20, t=20, b=20),
            legend_title_text="UF",
        )

        # Centralizar o gráfico
        _c1, _c2, _c3 = st.columns([1, 2, 1])
        with _c2:
            st.plotly_chart(fig_state, use_container_width=True)
else:
    st.info("Coluna 'UF' não encontrada.")

# =============================
# Tabela + Download
# =============================
st.subheader("Tabela (filtrada)")

df_show = df_f.copy()

# remove colunas internas
for internal in ["_data"]:
    if internal in df_show.columns:
        df_show = df_show.drop(columns=[internal])

# limitar colunas exibidas
if "show_cols" in locals() and show_cols:
    cols_ok = [c for c in show_cols if c in df_show.columns]
    if cols_ok:
        df_show = df_show[cols_ok]

st.dataframe(df_show, use_container_width=True, height=520)
download_csv(df_show, filename="relatorio_instalacoes_filtrado.csv")
