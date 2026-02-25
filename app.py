import re
from datetime import date
from typing import Optional

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
from google.oauth2.service_account import Credentials


# =============================
# Paleta de cores personalizada
# =============================
COR1 = "#1896D8"  # destaques
COR2 = "#CC1B63"  # alerta (>= 26% reagend.)
COR3 = "#342B38"  # (não usado agora)

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

# Leitura + escrita (para cadastro de instalações)
SCOPES_READWRITE = ["https://www.googleapis.com/auth/spreadsheets"]

def _get_gspread_client(scopes: list[str]):
    """Cria cliente gspread usando Service Account (st.secrets["google_service_account"])."""
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"],
        scopes=scopes,
    )
    return gspread.authorize(creds)


def append_row_to_sheet(
    spreadsheet_id: str,
    sheet_name: Optional[str],
    values_by_header: dict[str, object],
) -> None:
    """Insere uma nova linha no Google Sheets, respeitando a ordem do cabeçalho existente.

    - Lê a 1ª linha (headers) da aba
    - Monta a linha nova na mesma ordem
    - Preenche vazio para colunas não informadas
    """
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
        # Mantém compatibilidade com headers com espaços invisíveis
        key = h.strip()
        v = values_by_header.get(key, "")
        row.append("" if v is None else str(v))

    # Procura a primeira linha vazia usando a coluna A (ex.: "Data")
    # Obs: usando range fixo, conseguimos "ver" buracos.
    col_a = ws.get(f"A2:A{ws.row_count}")  # lista de linhas; vazias podem vir como [] ou não vir

    # Normaliza para ter exatamente (row_count - 1) itens
    # (cada item é [] ou ["valor"])
    if len(col_a) < (ws.row_count - 1):
        col_a = col_a + [[]] * ((ws.row_count - 1) - len(col_a))

    first_empty_row = None
    for offset, cell in enumerate(col_a, start=2):  # começa na linha 2
        val = ""
        if cell and len(cell) > 0:
            val = str(cell[0]).strip()
        if val == "":
            first_empty_row = offset
            break

    if first_empty_row is None:
        ws.append_row(row, value_input_option="USER_ENTERED")
    else:
        ws.insert_row(row, index=first_empty_row, value_input_option="USER_ENTERED")


# Ajuste aqui se os nomes das colunas na planilha forem diferentes
COL_DATA = "Data"
COL_HORA_INICIO = "Início"
COL_HORA_FIM = "Término"
COL_DURACAO = "Duração"  # usado no "Tempo Médio"
COL_VALOR_INST = "Valor da instalação"
COL_MODALIDADE = "Modalidade"
COL_TECNICO = "Técnico"  # ranking de técnicos (fallbacks abaixo)
COL_CONSULTOR = "Consultor"
COL_CLIENTE = "Cliente"
COL_UF = "UF"
COL_STATUS = "Status"
COL_CIDADE = "Cidade"
COL_QT_QUIOSQUE = "Quantidade Quiosque"
COL_QT_PLAYERS = "Quantidade Players"
COL_PLANO = "Plano"




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




def parse_brl_money(value) -> Optional[float]:
    """Converte valores monetários BRL para float.

    Aceita tanto:
      - números (ex.: 500)
      - strings formatadas (ex.: 'R$ 500,00', '1.234,56')
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    # Já numérico
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)

    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None

    # Remove símbolos e espaços (inclui NBSP do Sheets)
    s = s.replace("\u00A0", " ").replace(" ", "")
    s = s.replace("R$", "").replace("r$", "")
    # Remove separador de milhar e ajusta decimal pt-BR
    s = s.replace(".", "").replace(",", ".")
    v = pd.to_numeric(s, errors="coerce")
    if pd.isna(v):
        return None
    return float(v)


def format_number_pt(value: Optional[float], decimals: int = 1) -> str:
    """Formata número no padrão pt-BR (1.234,5)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        v = float(value)
    except Exception:
        return "—"
    s = f"{v:,.{decimals}f}"
    # Python usa ',' para milhar e '.' para decimal. Troca para pt-BR.
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def format_currency_brl(value: Optional[float]) -> str:
    """Formata moeda em BRL (R$ 1.234,56)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        v = float(value)
    except Exception:
        return "—"
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


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


def cliente_base(nome: object) -> str:
    """Normaliza nome do cliente removendo sufixos numéricos.

    Exemplos:
      - "Mercantil 01" -> "Mercantil"
      - "Mercantil-2"  -> "Mercantil"
      - "Cliente #12"  -> "Cliente"
    """
    if nome is None or (isinstance(nome, float) and pd.isna(nome)):
        return ""
    s = str(nome).strip()
    if not s:
        return ""
    # remove sufixos numéricos no final (com separadores comuns)
    s = re.sub(r"\s*(?:[-#]|nº|no\.?|num\.?|\.)?\s*\d+\s*$", "", s, flags=re.IGNORECASE)
    # remove espaços duplos
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def normalize_status(value: object) -> str:
    """Normaliza o status para comparação (sem acentos, minúsculo)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip().lower()
    if not s:
        return ""
    # remove acentos comuns (concluído/concluido etc.)
    s = (
        s.replace("á", "a").replace("à", "a").replace("ã", "a").replace("â", "a")
        .replace("é", "e").replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o").replace("ô", "o").replace("õ", "o")
        .replace("ú", "u")
        .replace("ç", "c")
    )
    return s


def is_concluido(status_value: object) -> bool:
    s = normalize_status(status_value)
    return s.startswith("conclu")


def is_cancelado(status_value: object) -> bool:
    s = normalize_status(status_value)
    return s.startswith("cancel")


def is_reagendar(status_value: object) -> bool:
    s = normalize_status(status_value)
    return "reagend" in s or s.startswith("reagendar") or s.startswith("reagendado")


def split_tecnicos(value: object) -> list[str]:
    """Divide campo de técnico(s) em lista (suporta vírgula, ';', '/', '\\' e ' e ')."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    s = re.sub(r"\s*(,|;|/|\\)\s*", ",", s)
    s = re.sub(r"\s+e\s+", ",", s, flags=re.IGNORECASE)
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p]


def month_label_pt(ym: str) -> str:
    """Converte 'YYYY-MM' em rótulo pt-br curto (ex.: '2026-01' -> 'jan/2026')."""
    meses = [
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    ]
    try:
        y, m = ym.split("-")
        mi = int(m)
        return f"{meses[mi-1]}/{y}"
    except Exception:
        return ym


def kpi_card(label: str, value: str, color: str = COR1) -> None:
    st.markdown(
        f"""
        <div style="
            padding: 10px 12px;
            border-radius: 10px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            ">
            <div style="font-size: 14px; opacity: 0.85;">{label}</div>
            <div style="font-size: 34px; font-weight: 800; color: {color}; line-height: 1.1;">{value}</div>
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
        color_discrete_sequence=[COR1],
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
        color_discrete_sequence=[COR1],
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

    Importante: usamos get_all_values() para NÃO perder linhas após linhas em branco.
    (get_all_records() pode ignorar linhas vazias e, dependendo da estrutura da planilha,
    acabar não trazendo linhas depois de "buracos".)

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

    values = ws.get_all_values()  # inclui linhas vazias no meio
    if not values or len(values) < 2:
        return pd.DataFrame()

    headers = [h.strip() for h in values[0]]
    # Remove cabeçalhos vazios no fim (caso exista coluna extra sem nome)
    while headers and headers[-1] == "":
        headers.pop()

    rows = values[1:]
    norm_rows = []
    n = len(headers)
    for r in rows:
        r = r[:n] + [""] * max(0, n - len(r))
        # Mantém a leitura passando por linhas totalmente vazias, mas não as inclui no DF
        if all(str(x).strip() == "" for x in r):
            continue
        norm_rows.append(r)

    return pd.DataFrame(norm_rows, columns=headers)


# =============================
# Header + Load
# =============================
if st.session_state.get("view_mode", "RELATÓRIO") == "RELATÓRIO":
    st.title("📊 Relatório de Instalações NextQS")

SPREADSHEET_ID = st.secrets.get("spreadsheet_id", "")
SHEET_NAME = None  # definido pelo seletor de dashboard

if not SPREADSHEET_ID:
    st.error("Faltou configurar `spreadsheet_id` nos secrets.")
    st.stop()

with st.sidebar:
    # Navegação (botões)
    if "view_mode" not in st.session_state:
        st.session_state.view_mode = "RELATÓRIO"

    if st.button("RELATÓRIO", use_container_width=True):
        st.session_state.view_mode = "RELATÓRIO"
    if st.button("CADASTRAR INSTALAÇÃO", use_container_width=True):
        st.session_state.view_mode = "CADASTRAR INSTALAÇÃO"

    st.divider()

with st.sidebar:
    st.header("Filtros")

    # Seletor de dashboard (cada opção aponta para uma aba da planilha)
    DASHBOARDS = {
        "Instalações": "Instalacoes_2026",
        # Adicione outros dashboards aqui, por exemplo:
        # "Reagendamentos": "Reagendamentos_2026",
        # "Financeiro": "Financeiro_2026",
    }

    selected_dashboard = st.radio(
        "Dashboard",
        options=list(DASHBOARDS.keys()),
        index=0,
        label_visibility="collapsed",
    )

    SHEET_NAME = DASHBOARDS[selected_dashboard]


# =============================
# Tela: Cadastro de Instalação
# =============================
from datetime import datetime, timedelta


def _digits_only(x: str) -> str:
    return re.sub(r"\D", "", x or "")


def _mask_date_ddmmyyyy(x: str) -> str:
    """Usuário digita só números (ex: 06022026) e o campo vira 06/02/2026."""
    d = _digits_only(x)[:8]
    if len(d) <= 2:
        return d
    if len(d) <= 4:
        return f"{d[:2]}/{d[2:4]}"
    return f"{d[:2]}/{d[2:4]}/{d[4:8]}"


def _mask_time_hhmm(x: str) -> str:
    """Usuário digita só números (ex: 1000) e o campo vira 10:00."""
    d = _digits_only(x)[:4]
    if len(d) <= 2:
        return d
    return f"{d[:2]}:{d[2:4]}"


def _parse_date_ddmmyyyy(s: str):
    """Aceita dd/mm/aaaa OU 8 dígitos (ddmmaaaa)."""
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
    """Aceita HH:MM OU 4 dígitos (hhmm)."""
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
    """Calcula duração HH:MM. Erro se término < início."""
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
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def _parse_brl_number_str(s: str):
    # Aceita números com vírgula; converte usando helper existente
    return parse_brl_money(s)

if st.session_state.get("view_mode") == "CADASTRAR INSTALAÇÃO":
    st.title("📝 Cadastrar Instalação")
    st.caption(f"Aba de destino: **{SHEET_NAME}**")

    # Máscara simples via on_change (fora de st.form, então é permitido)
    def _on_data_change():
        st.session_state.data_txt = _mask_date_ddmmyyyy(st.session_state.get("data_txt", ""))

    def _on_inicio_change():
        st.session_state.inicio_txt = _mask_time_hhmm(st.session_state.get("inicio_txt", ""))

    def _on_termino_change():
        st.session_state.termino_txt = _mask_time_hhmm(st.session_state.get("termino_txt", ""))

    c1, c2, c3 = st.columns(3)
    with c1:
        data_txt = st.text_input("Data", placeholder="dd/mm/aaaa", key="data_txt", max_chars=10, on_change=_on_data_change)
        inicio_txt = st.text_input("Início", placeholder="hh:mm", key="inicio_txt", max_chars=5, on_change=_on_inicio_change)
        termino_txt = st.text_input("Término", placeholder="hh:mm", key="termino_txt", max_chars=5, on_change=_on_termino_change)

    with c2:
        modalidade = st.selectbox(
            "Modalidade",
            ["Remota", "Presencial", "Híbrida", "Evento", "Apresentação", "Boas-vindas"],
        )
        consultor = st.selectbox(
            "Consultor",
            ["Shimada", "André", "Jefferson", "Sandro", "Renato"],
        )
        tecnicos_sel = st.multiselect(
            "Técnico(s)",
            ["Davi", "Vinícius", "Marcos", "Ryen", "Jonathan", "Renato", "Fábio"],
            default=[],
        )

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
        emissor_tipo = st.selectbox(
            "Emissor de senhas",
            ["Quiosque de chão", "Quiosque de mesa", "Portátil", "Software", "Sem emissor"],
        )
        emissor_cliente = st.selectbox("Emissor cliente", ["FALSE", "TRUE"])
        emissores_qtd = st.number_input("Emissores (quantidade)", min_value=0, step=1, value=0)

    with c6:
        player_tipo = st.selectbox(
            "Player",
            ["Stick Player", "MiniPC", "Software", "Sem player"],
        )
        player_cliente = st.selectbox("Player cliente", ["FALSE", "TRUE"])
        players_qtd = st.number_input("Players (quantidade)", min_value=0, step=1, value=0)

    st.divider()

    c7, c8, c9 = st.columns(3)
    with c7:
        plano_opts = ["", "TB", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T15", "Locação"]
        plano = st.selectbox(
            "Plano",
            plano_opts,
            index=0,
            key="plano_sel_v3",
        )

    with c8:
        valor_txt = st.text_input("Valor da instalação", placeholder="500,00")

    with c9:
        motivo_reag = st.selectbox(
            "Motivo reagendamento",
            ["", "Finalizar treinamento", "Finalizar instalação", "Infraestrutura", "Stick", "Totem", "Cancelamento"],
            index=0,
        )

    observacao_txt = st.text_area("Observação")

    salvar = st.button("Salvar na planilha", use_container_width=True)

    if salvar:
        errors = []

        d = _parse_date_ddmmyyyy(data_txt)
        if not d:
            errors.append("Data inválida (use dd/mm/aaaa, ex.: 05/01/2026).")

        # Permite apenas números e "/" na Data
        if data_txt.strip() and not re.fullmatch(r"[0-9/]+", data_txt.strip()):
            errors.append("Data: use apenas números e '/'.")

        if _parse_time_hhmm(inicio_txt.strip()) is None:
            errors.append("Início inválido (use HH:MM, ex.: 13:20).")
        if _parse_time_hhmm(termino_txt.strip()) is None:
            errors.append("Término inválido (use HH:MM, ex.: 15:10).")

        # Permite apenas números e ":" nos horários
        if inicio_txt.strip() and not re.fullmatch(r"[0-9:]+", inicio_txt.strip()):
            errors.append("Início: use apenas números e ':'.")
        if termino_txt.strip() and not re.fullmatch(r"[0-9:]+", termino_txt.strip()):
            errors.append("Término: use apenas números e ':'.")

        uf_clean = (uf_txt or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", uf_clean):
            errors.append("UF inválida (use apenas 2 letras, ex.: SP).")

        valor_num = _parse_brl_number_str(valor_txt)
        if valor_txt.strip() and valor_num is None:
            errors.append("Valor da instalação inválido (use números e vírgula, ex.: 500,00).")

        # Calcula duração (HH:MM) a partir de Início e Término
        duracao_calc = None
        if not errors:
            try:
                duracao_calc = _duration_hhmm(inicio_txt.strip(), termino_txt.strip())
            except Exception as ex:
                errors.append(str(ex))

        if errors:
            for e in errors:
                st.error(e)
        else:
            values_by_header = {
                "Data": d.strftime("%d/%m/%Y"),
                "Início": inicio_txt.strip(),
                "Término": termino_txt.strip(),
                "Modalidade": modalidade,
                "Consultor": consultor,
                "Cliente": cliente_txt.strip(),
                "Emissor de senhas": emissor_tipo,
                "Emissor cliente": emissor_cliente,
                "Emissores": int(emissores_qtd),
                "Quantidade Quiosque": int(emissores_qtd),
                "Player": player_tipo,
                "Player cliente": player_cliente,
                "Players": int(players_qtd),
                "Quantidade Players": int(players_qtd),
                "UF": uf_clean,
                "Cidade": cidade_txt.strip(),
                "Técnico": ", ".join(tecnicos_sel) if tecnicos_sel else "",
                "Status": status,
                "CV": cv_txt.strip(),
                "Plano": plano,
                "CV Instalação": cv_inst_txt.strip(),
                "Valor da instalação": (valor_txt.strip() if valor_txt.strip() else ""),
                "Motivo reagendamento": (motivo_reag.strip() if motivo_reag else ""),
                "Observação": observacao_txt.strip(),
                "Duração": (duracao_calc or ""),
            }

            try:
                append_row_to_sheet(SPREADSHEET_ID, SHEET_NAME, values_by_header)
                st.success("✅ Registro salvo na planilha!")
                st.info("Se você voltar para **RELATÓRIO**, o dashboard vai recarregar com os novos dados.")
            except Exception as ex:
                st.error(f"Não foi possível salvar na planilha: {ex}")

    st.stop()



# Sempre relê a planilha (sincronismo a cada alteração de filtro)
df_raw = read_sheet(SPREADSHEET_ID, SHEET_NAME)

if df_raw.empty:
    st.warning("A planilha não retornou dados.")
    st.stop()

df = df_raw.copy()

# Normaliza nomes de colunas (evita espaços invisíveis no cabeçalho)
df.columns = df.columns.str.strip()

# Normalização de data
df["_data"] = to_date_series(df[COL_DATA]) if safe_col(df, COL_DATA) else pd.NaT
has_valid_dates = df["_data"].notna().any()
# Referências de data
# - today_*: usado para filtros 'Este mês' e 'Este ano' (calendário real)
# - data_ref_*: usado apenas para escolher defaults do filtro 'Personalizado'
today_ts = pd.Timestamp.today()
today_date = today_ts.date()

data_ref_dt = df["_data"].dropna().max() if has_valid_dates else today_ts

data_ref_year = int(data_ref_dt.year)
data_ref_month = int(data_ref_dt.month)

# =============================
# Sidebar: filtros principais (radio)
# =============================
with st.sidebar:
    period_option = st.radio(
        "Período",
        options=["Este mês", "Este ano", "Personalizado"],
        index=0,  # padrão: Este mês
        label_visibility="visible",
    )

    # Se Personalizado: escolhe ano/mês disponíveis na planilha
    sel_custom_year = None
    sel_custom_month = None
    if period_option == "Personalizado":
        if has_valid_dates:
            _dates_all = df["_data"].dropna()
            if _dates_all.empty:
                st.info("Sem datas válidas para filtro personalizado.")
            else:
                years = sorted(_dates_all.dt.year.unique().tolist())
                year_default = data_ref_year if data_ref_year in years else years[-1]
                sel_custom_year = st.selectbox("Ano", options=years, index=years.index(year_default))

                months_avail = (
                    _dates_all[_dates_all.dt.year == sel_custom_year]
                    .dt.month
                    .unique()
                    .tolist()
                )
                months_avail = sorted([int(m) for m in months_avail])
                month_names = [
                    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
                    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
                ]
                month_options = [f"{m:02d} - {month_names[m-1]}" for m in months_avail]
                # tenta manter o mês de referência
                m_default = data_ref_month if data_ref_month in months_avail else months_avail[-1]
                sel_custom_month = st.selectbox(
                    "Mês",
                    options=month_options,
                    index=months_avail.index(m_default),
                )
        else:
            st.info("Sem coluna de data válida para filtro personalizado.")

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
        # Cliente (considera nomes com numeração como o mesmo cliente)
        if safe_col(df, COL_CLIENTE):
            df["_cliente_base"] = df[COL_CLIENTE].map(cliente_base)
            cliente_opts = sorted([c for c in df["_cliente_base"].dropna().astype(str).unique().tolist() if c.strip()])
            sel_cliente = st.multiselect("Cliente", options=cliente_opts)
        else:
            sel_cliente = []
            st.caption(f"Coluna ausente: {COL_CLIENTE}")

        # Plano
        sel_plano = multiselect_filter("Plano", COL_PLANO)

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
    # Para "Este mês" e "Este ano" queremos considerar o período inteiro
    # (incluindo datas futuras já agendadas), então filtramos por mês/ano.
    if period_option == "Este mês":
        y = int(today_date.year)
        m = int(today_date.month)
        df_f = df_f[(df_f["_data"].dt.year == y) & (df_f["_data"].dt.month == m)]

    elif period_option == "Este ano":
        y = int(today_date.year)
        df_f = df_f[df_f["_data"].dt.year == y]

    else:  # Personalizado (mês fechado)
        if sel_custom_year is None or sel_custom_month is None:
            y = int(today_date.year)
            m = int(today_date.month)
            df_f = df_f[(df_f["_data"].dt.year == y) & (df_f["_data"].dt.month == m)]
        else:
            try:
                m = int(str(sel_custom_month).split("-")[0].strip())
            except Exception:
                m = data_ref_month
            y = int(sel_custom_year)
            df_f = df_f[(df_f["_data"].dt.year == y) & (df_f["_data"].dt.month == int(m))]


def apply_multiselect(df_in: pd.DataFrame, col: str, selected: list[str]) -> pd.DataFrame:
    if not selected or col not in df_in.columns:
        return df_in
    return df_in[df_in[col].astype(str).isin(selected)]


df_f = apply_multiselect(df_f, COL_MODALIDADE, sel_modalidade if "sel_modalidade" in locals() else [])
df_f = apply_multiselect(df_f, COL_UF, sel_uf if "sel_uf" in locals() else [])
df_f = apply_multiselect(df_f, COL_CIDADE, sel_cidade if "sel_cidade" in locals() else [])

# Cliente (filtra pelo nome-base)
if "sel_cliente" in locals() and sel_cliente and safe_col(df_f, COL_CLIENTE):
    df_f["_cliente_base"] = df_f[COL_CLIENTE].map(cliente_base)
    df_f = df_f[df_f["_cliente_base"].astype(str).isin([str(x) for x in sel_cliente])]

# Plano
df_f = apply_multiselect(df_f, COL_PLANO, sel_plano if "sel_plano" in locals() else [])

if "tecnico_col" in locals() and tecnico_col:
    # Quando há mais de um técnico na mesma instalação, consideramos "match" se QUALQUER técnico estiver selecionado.
    if "sel_tecnico" in locals() and sel_tecnico:
        sel_set = set(str(x).strip() for x in sel_tecnico if str(x).strip())
        if sel_set:
            df_f = df_f[df_f[tecnico_col].map(lambda v: bool(set(split_tecnicos(v)) & sel_set))]
if "consultor_col" in locals() and consultor_col:
    df_f = apply_multiselect(df_f, consultor_col, sel_consultor if "sel_consultor" in locals() else [])

# =============================
# KPIs (destaques)
# =============================
status_col_main = first_existing_col(df_f, [COL_STATUS, 'Status da Instalação', 'Status Instalação', 'Situacao', 'Situação'])
instalacoes_concluidas = int(df_f[status_col_main].map(is_concluido).sum()) if status_col_main else 0

tempo_medio_str = "—"
if safe_col(df_f, COL_DURACAO):
    mins = df_f[COL_DURACAO].map(_parse_duration_to_minutes).dropna()
    tempo_medio_str = format_minutes_pt(mins.mean()) if not mins.empty else "—"

modalidade_mais_comum = mode_value(df_f[COL_MODALIDADE]) if safe_col(df_f, COL_MODALIDADE) else "—"

reag_rate = get_reagendamento_rate(df_f)
taxa_reag = f"{reag_rate*100:.1f}%" if reag_rate is not None else "—"

taxa_reag_color = COR2 if (reag_rate is not None and reag_rate >= 0.26) else COR1
k1, k2, k3, k4 = st.columns(4)
with k1:
    kpi_card("Instalações Concluídas", f"{instalacoes_concluidas}", color=COR1)
with k2:
    kpi_card("Tempo Médio", tempo_medio_str, color=COR1)
with k3:
    kpi_card("Modalidade mais comum", modalidade_mais_comum, color=COR1)
with k4:
    kpi_card("Taxa de Reagendamentos", taxa_reag, color=taxa_reag_color)


# Espaço entre a primeira e a segunda linha de KPIs
st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

# --- Novos destaques ---
faturamento_total = 0.0
if safe_col(df_f, COL_VALOR_INST):
    valores = df_f[COL_VALOR_INST].map(parse_brl_money).dropna()
    faturamento_total = float(valores.sum()) if not valores.empty else 0.0


total_minutes_sum = 0.0
if safe_col(df_f, COL_DURACAO):
    mins_all = df_f[COL_DURACAO].map(_parse_duration_to_minutes).dropna()
    total_minutes_sum = float(mins_all.sum()) if not mins_all.empty else 0.0

horas_totais = total_minutes_sum / 60.0 if total_minutes_sum else 0.0
valor_por_hora = (faturamento_total / horas_totais) if horas_totais > 0 else None

cliente_mais_instalacoes = "—"
if safe_col(df_f, COL_CLIENTE):
    tmpc = df_f[COL_CLIENTE].map(cliente_base)
    tmpc = tmpc.dropna().astype(str).str.strip()
    tmpc = tmpc[tmpc != ""]
    if not tmpc.empty:
        cliente_mais_instalacoes = tmpc.value_counts().index[0]

k5, k6, k7, k8 = st.columns(4)
with k5:
    kpi_card("Faturamento Total", format_currency_brl(faturamento_total), color=COR1)
with k6:
    kpi_card("Horas Totais", f"{format_number_pt(horas_totais, 1)} h" if horas_totais else "0,0 h", color=COR1)
with k7:
    kpi_card("Valor por Hora", format_currency_brl(valor_por_hora) if valor_por_hora is not None else "—", color=COR1)
with k8:
    kpi_card("Cliente com mais instalações", cliente_mais_instalacoes, color=COR1)


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

        # Rótulos em português e força exibição de todos os meses no eixo X
        dfm["Mês (rótulo)"] = dfm["Mês"].map(month_label_pt)

        fig = px.bar(
            dfm,
            x="Mês",
            y="Instalações",
            text="Instalações",
            template="plotly_dark",
            color_discrete_sequence=[COR1],
        )
        fig.update_traces(textposition="outside", cliponaxis=False)
        fig.update_layout(
            height=360,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title="",
            yaxis_title="Instalações",
        )
        fig.update_xaxes(
            tickangle=-35,
            tickmode="array",
            tickvals=dfm["Mês"].tolist(),
            ticktext=dfm["Mês (rótulo)"].tolist(),
        )
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
        # Bucket do status conforme regra:
        # - Concluído
        # - Reagendar - Cortesia (Status Reagendar e Valor da instalação == 0)
        # - Reagendar (Status Reagendar e Valor da instalação != 0)
        # - Cancelado
        def _bucket_status(row: pd.Series) -> str:
            stv = row.get(status_col, "")
            if is_concluido(stv):
                return "Concluído"
            if is_cancelado(stv):
                return "Cancelado"
            if is_reagendar(stv):
                v = parse_brl_money(row.get(COL_VALOR_INST, None))
                v = 0.0 if v is None else float(v)
                return "Reagendar - Cortesia" if v == 0 else "Reagendar"
            return ""

        s_bucket = df_f.apply(_bucket_status, axis=1)
        s_bucket = s_bucket[s_bucket != ""]

        if s_bucket.empty:
            st.info("Sem dados de status para o gráfico.")
        else:
            # Mantém apenas as 4 categorias esperadas (e garante que não apareçam outras)
            order = ["Concluído", "Reagendar - Cortesia", "Reagendar", "Cancelado"]
            counts = s_bucket.value_counts()
            dfc = pd.DataFrame({"Status": order, "Instalações": [int(counts.get(k, 0)) for k in order]})
            dfc = dfc[dfc["Instalações"] > 0]

            fig = px.bar(
                dfc,
                x="Status",
                y="Instalações",
                text="Instalações",
                template="plotly_dark",
                color_discrete_sequence=[COR1],
            )
            fig.update_traces(textposition="outside", cliponaxis=False)
            fig.update_layout(
                height=360,
                margin=dict(l=20, r=20, t=20, b=20),
                xaxis_title="",
                yaxis_title="Instalações",
            )
            fig.update_xaxes(tickangle=-20)
            st.plotly_chart(fig, use_container_width=True)
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
        tech_series = (
            df_f[tecnico_col_rank]
            .map(split_tecnicos)
            .explode()
            .dropna()
            .astype(str)
            .str.strip()
        )
        tech_series = tech_series[tech_series != ""]
        tech_counts = tech_series.value_counts().reset_index()
        tech_counts.columns = ["Técnico", "Instalações"]

        fig_tec = px.bar(
            tech_counts,
            x="Técnico",
            y="Instalações",
            text="Instalações",
            template="plotly_dark",
            color_discrete_sequence=[COR1],
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
# Clientes (contagem) + Motivo de Reagendamento (lado a lado)
# =============================
cl_left, cl_right = st.columns(2)

with cl_left:
    st.subheader("Clientes e quantidade de Instalações")
    if safe_col(df_f, COL_CLIENTE):
        tmp = df_f.copy()
        tmp["_cliente_base"] = tmp[COL_CLIENTE].map(cliente_base)

        status_col_tbl = first_existing_col(
            tmp,
            [
                COL_STATUS,
                "Status da Instalação",
                "Status Instalação",
                "Situacao",
                "Situação",
            ],
        )
        if not status_col_tbl:
            st.info("Coluna de status não encontrada (ex.: 'Status').")
        else:
            tmp["_is_concluido"] = tmp[status_col_tbl].map(is_concluido)
            tmp["_is_reagendar"] = tmp[status_col_tbl].map(is_reagendar)
            tmp["_is_cancelado"] = tmp[status_col_tbl].map(is_cancelado)

            base = tmp["_cliente_base"].dropna().astype(str).str.strip()
            base = base[base != ""]
            tmp = tmp.loc[base.index].copy()

            if tmp.empty:
                st.info("Sem dados de cliente para listar.")
            else:
                df_clientes = (
                    tmp.groupby("_cliente_base", dropna=False)
                    .agg(
                        Concluídas=("_is_concluido", "sum"),
                        Reagendadas=("_is_reagendar", "sum"),
                        Canceladas=("_is_cancelado", "sum"),
                    )
                    .reset_index()
                    .rename(columns={"_cliente_base": "Cliente"})
                )
                df_clientes["Total"] = (
                    df_clientes["Concluídas"]
                    + df_clientes["Reagendadas"]
                    + df_clientes["Canceladas"]
                )
                df_clientes = df_clientes.sort_values(["Total", "Cliente"], ascending=[False, True])

                st.dataframe(df_clientes, use_container_width=True, height=360)

                tot_conc = int(df_clientes["Concluídas"].sum())
                tot_reag = int(df_clientes["Reagendadas"].sum())
                tot_canc = int(df_clientes["Canceladas"].sum())
                st.markdown(
                    f"**Totais (no período filtrado):** Concluídas: **{tot_conc}** · "
                    f"Reagendadas: **{tot_reag}** · Canceladas: **{tot_canc}**"
                )
    else:
        st.info("Coluna 'Cliente' não encontrada.")

with cl_right:
    st.subheader("Motivo de Reagendamento")
    motivo_col = first_existing_col(
        df_f,
        [
            "Motivo reagendamento",
            "Motivo Reagendamento",
            "Motivo do reagendamento",
            "Motivo do Reagendamento",
            "Motivo",
        ],
    )
    if motivo_col:
        s_motivo = df_f[motivo_col].dropna().astype(str).str.strip()
        s_motivo = s_motivo[s_motivo != ""]
        if s_motivo.empty:
            st.info("Sem motivos preenchidos.")
        else:
            bar_chart_counts(s_motivo, top_n=25, y_label="Ocorrências")
    else:
        st.info("Coluna de motivo de reagendamento não encontrada.")

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
