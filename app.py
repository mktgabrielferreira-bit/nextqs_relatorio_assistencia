import streamlit as st
import pandas as pd
import gspread
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials


# =============================
# Config
# =============================
st.set_page_config(page_title="Relatório de Assistência", layout="wide")

SCOPES_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Ajuste aqui se os nomes das colunas na planilha forem diferentes
COL_DATA = "Data agendada"
COL_HORA_INICIO = "Hora inicio"
COL_HORA_FIM = "Hora fim"
COL_MODALIDADE = "Modalidade"
COL_CONSULTOR = "Consultor"
COL_CLIENTE = "Cliente"
COL_UF = "UF"
COL_CIDADE = "Cidade"
COL_QT_QUIOSQUE = "Quantidade Quiosque"
COL_QT_PLAYERS = "Quantidade Players"


# =============================
# Password Gate (opcional)
# =============================
# Aceita dois nomes de secret para compatibilidade:
# - app_password (padrão deste app)
# - SENHA_DASH (padrão de outros dashboards)
APP_PASSWORD = st.secrets.get("app_password") or st.secrets.get("SENHA_DASH")


def require_password() -> None:
    """Bloqueia o app até o usuário informar a senha correta."""
    if not APP_PASSWORD:
        return  # senha não configurada -> acesso livre

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    st.markdown(
        """
        <div style="text-align:center; padding: 48px 0 8px 0;">
            <h1 style="font-size: 44px; margin-bottom: 6px;">🔒 Acesso restrito</h1>
            <p style="opacity:0.75; font-size: 18px;">Digite a senha para acessar o relatório</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Centraliza e deixa o campo menor
    left, mid, right = st.columns([2, 3, 2])
    with mid:
        pwd = st.text_input("Senha de acesso", type="password")
        if st.button("Entrar", use_container_width=True):
            if pwd == APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Senha incorreta.")

    st.stop()


require_password()

# =============================
# Helpers
# =============================
def safe_col(df: pd.DataFrame, col: str) -> bool:
    return col in df.columns

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

def bar_chart_counts(series: pd.Series, title: str, top_n: int = 10) -> None:
    if series is None:
        st.info("Sem dados para o gráfico.")
        return

    s = series.dropna().astype(str)
    counts = s.value_counts().head(top_n)

    if counts.empty:
        st.info("Sem dados para o gráfico.")
        return

    fig = plt.figure()
    plt.title(title)
    plt.bar(counts.index.astype(str), counts.values)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    st.pyplot(fig)

@st.cache_data(ttl=300, show_spinner=False)
def load_sheet(spreadsheet_id: str, sheet_name: str | None) -> pd.DataFrame:
    """
    Lê dados do Google Sheets via Service Account (st.secrets["google_service_account"]).
    Cache: 5 minutos.
    """
    creds = Credentials.from_service_account_info(
        st.secrets["google_service_account"],
        scopes=SCOPES_READONLY,
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    # Se o nome da aba estiver vazio ou não existir, cai para a primeira
    try:
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
    except Exception:
        ws = sh.sheet1

    records = ws.get_all_records()
    return pd.DataFrame(records)


# =============================
# Header + Load
# =============================
st.title("Relatório de Assistência")

SPREADSHEET_ID = st.secrets.get("spreadsheet_id", "")
SHEET_NAME_DEFAULT = st.secrets.get("sheet_name", "2026")

if not SPREADSHEET_ID:
    st.error("Faltou configurar `spreadsheet_id` nos secrets.")
    st.stop()

with st.sidebar:
    # Botão de sair (opcional) - aparece somente após autenticação
    if st.session_state.get("authenticated"):
        if st.button("Sair", use_container_width=True):
            st.session_state.authenticated = False
            st.rerun()

    st.header("Configurações")
    sheet_name = st.text_input(
        "Nome da aba (worksheet)",
        value=SHEET_NAME_DEFAULT,
        help="Ex.: 2026. Se estiver vazio/errado, o app usa a primeira aba.",
    ).strip()

df_raw = load_sheet(SPREADSHEET_ID, sheet_name)

if df_raw.empty:
    st.warning("A planilha não retornou dados.")
    st.stop()

df = df_raw.copy()

# Normalizações básicas
if safe_col(df, COL_DATA):
    df["_data"] = to_date_series(df[COL_DATA])
else:
    df["_data"] = pd.NaT


# =============================
# Sidebar Filters
# =============================
with st.sidebar:
    st.subheader("Filtros")

    # Período (se tiver data)
    if df["_data"].notna().any():
        min_date = df["_data"].min().date()
        max_date = df["_data"].max().date()
        date_range = st.date_input(
            "Período (Data agendada)",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start_date, end_date = date_range
        else:
            start_date, end_date = min_date, max_date
    else:
        start_date = end_date = None
        st.caption("Sem coluna de data válida para filtro.")

    def multiselect_filter(label: str, col: str) -> list[str]:
        if not safe_col(df, col):
            st.caption(f"Coluna ausente: {col}")
            return []
        vals = sorted(df[col].dropna().astype(str).unique().tolist())
        return st.multiselect(label, options=vals)

    sel_modalidade = multiselect_filter("Modalidade", COL_MODALIDADE)
    sel_consultor = multiselect_filter("Consultor", COL_CONSULTOR)
    sel_uf = multiselect_filter("UF", COL_UF)
    sel_cidade = multiselect_filter("Cidade", COL_CIDADE)
    sel_cliente = multiselect_filter("Cliente", COL_CLIENTE)

    st.divider()
    st.subheader("Exibição")
    show_cols = st.multiselect(
        "Colunas na tabela",
        options=[c for c in df.columns if not c.startswith("_")],
        default=[c for c in df.columns if not c.startswith("_")],
    )


# =============================
# Apply Filters
# =============================
df_f = df.copy()

if start_date and end_date and df_f["_data"].notna().any():
    df_f = df_f[
        (df_f["_data"].dt.date >= start_date) &
        (df_f["_data"].dt.date <= end_date)
    ]

def apply_multiselect(df_in: pd.DataFrame, col: str, selected: list[str]) -> pd.DataFrame:
    if not selected or col not in df_in.columns:
        return df_in
    return df_in[df_in[col].astype(str).isin(selected)]

df_f = apply_multiselect(df_f, COL_MODALIDADE, sel_modalidade)
df_f = apply_multiselect(df_f, COL_CONSULTOR, sel_consultor)
df_f = apply_multiselect(df_f, COL_UF, sel_uf)
df_f = apply_multiselect(df_f, COL_CIDADE, sel_cidade)
df_f = apply_multiselect(df_f, COL_CLIENTE, sel_cliente)


# =============================
# KPIs
# =============================
total_registros = len(df_f)
total_clientes = df_f[COL_CLIENTE].nunique() if safe_col(df_f, COL_CLIENTE) else 0
total_consultores = df_f[COL_CONSULTOR].nunique() if safe_col(df_f, COL_CONSULTOR) else 0
total_quiosques = sum_numeric(df_f, COL_QT_QUIOSQUE)
total_players = sum_numeric(df_f, COL_QT_PLAYERS)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Registros", f"{total_registros}")
k2.metric("Clientes (únicos)", f"{total_clientes}")
k3.metric("Consultores (únicos)", f"{total_consultores}")
k4.metric("Qtd. Quiosques (soma)", f"{total_quiosques}")
k5.metric("Qtd. Players (soma)", f"{total_players}")

st.caption(f"Fonte: Google Sheets | Aba: **{sheet_name or 'primeira aba'}**")


# =============================
# Charts
# =============================
c1, c2 = st.columns(2)
with c1:
    if safe_col(df_f, COL_UF):
        bar_chart_counts(df_f[COL_UF], "Top UFs (por quantidade de registros)", top_n=10)
    else:
        st.info("Coluna 'UF' não encontrada para gráfico.")

with c2:
    if safe_col(df_f, COL_CONSULTOR):
        bar_chart_counts(df_f[COL_CONSULTOR], "Top Consultores (por quantidade de registros)", top_n=10)
    else:
        st.info("Coluna 'Consultor' não encontrada para gráfico.")

c3, c4 = st.columns(2)
with c3:
    if safe_col(df_f, COL_MODALIDADE):
        bar_chart_counts(df_f[COL_MODALIDADE], "Modalidade (contagem)", top_n=10)
    else:
        st.info("Coluna 'Modalidade' não encontrada para gráfico.")

with c4:
    if df_f["_data"].notna().any():
        s = df_f["_data"].dt.date.value_counts().sort_index()
        fig = plt.figure()
        plt.title("Registros por dia (Data agendada)")
        plt.plot(list(s.index), list(s.values))
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        st.pyplot(fig)
    else:
        st.info("Sem coluna de data válida para série temporal.")


# =============================
# Table + Download
# =============================
st.subheader("Tabela (filtrada)")

df_show = df_f.copy()

# remove coluna interna
if "_data" in df_show.columns:
    df_show = df_show.drop(columns=["_data"])

# limitar colunas exibidas
if show_cols:
    # mantém apenas as colunas solicitadas (ignorando as que não existirem)
    cols_ok = [c for c in show_cols if c in df_show.columns]
    if cols_ok:
        df_show = df_show[cols_ok]

st.dataframe(df_show, use_container_width=True, height=520)

download_csv(df_show, filename="relatorio_assistencia_filtrado.csv")
