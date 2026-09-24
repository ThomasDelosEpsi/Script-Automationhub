import os
import time
import json
import re
import unicodedata
import requests
import pandas as pd
from sqlalchemy import create_engine, text as sqltext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import Table, Column, String, Text, MetaData
from pgvector.sqlalchemy import Vector
from dotenv import load_dotenv

load_dotenv()  # charge .env à la racine

# =============================
# Config depuis l'environnement
# =============================
UIPATH_TOKEN = os.getenv("UIPATH_ACCESS_TOKEN")             # "Bearer …"
UIPATH_BASE_URL = os.getenv("UIPATH_BASE_URL")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL")  # ex: "http://proxy.lyreco.com:8080"
REQUESTS_CA_BUNDLE = os.getenv("REQUESTS_CA_BUNDLE")  # cert corporate si besoin

if not all([UIPATH_TOKEN, UIPATH_BASE_URL, MISTRAL_API_KEY, DATABASE_URL]):
    raise SystemExit("❌ Manque une ou plusieurs variables d’environnement (UIPATH_* / MISTRAL_API_KEY / DATABASE_URL).")

# Proxy dict (facultatif)
proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# =======
# HTTP
# =======
def build_session():
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    if proxies:
        s.proxies = proxies
    if REQUESTS_CA_BUNDLE:
        s.verify = REQUESTS_CA_BUNDLE

    adapter = HTTPAdapter(max_retries=Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"])
    ))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

sess = build_session()

# ============ Constantes ============
EMBEDDING_API_URL = "https://api.mistral.ai/v1/embeddings"
HEADERS_UIPATH = {
    "Content-Type": "application/json",
    "Authorization": UIPATH_TOKEN,
    "x-ah-openapi-auth": "openapi-token",
}

# Filtres métiers (laisser vide = pas de filtre)
EXCLUDED_PHASES = set()
EXCLUDED_STATUSES = set()

# ========= Helpers (texte & normalisation) =========
DEPT_ALIASES = {
    "FIN": "finance accounting controlling treasury",
    "FINANCE": "finance accounting controlling treasury",
    "IS": "it information systems",
    "IT": "it information technology",
    "HR": "human resources",
    "SCM": "supply chain",
    "SUPPLY": "supply chain logistics",
}

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()

def extract_draftjs_text(raw_description: str) -> str:
    """UiPath AH : certains champs sont du DraftJS JSON -> texte."""
    if not raw_description:
        return ""
    try:
        draft = json.loads(raw_description)
        return "\n".join(block.get("text", "") for block in draft.get("blocks", []))
    except Exception:
        return ""

def clean_text(s: str, max_len: int = 8000) -> str:
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s[:max_len]

def build_embedding_text(row: dict) -> str:
    """Texte riche: title + departments (expansion) + country + purpose."""
    title   = _norm(row.get("Subject Name Automation hub"))
    dept_raw= _norm(row.get("Departments"))
    country = _norm(row.get("Country"))
    purpose = _norm(row.get("Purpose"))

    # gère multiples départements séparés par ; , / | \
    dept_tokens = [t.strip() for t in re.split(r"[;,/|\\]", dept_raw) if t.strip()] or ([dept_raw] if dept_raw else [])
    dept_expanded = " ".join(DEPT_ALIASES.get(t.upper(), t) for t in dept_tokens)

    parts = []
    if title:         parts.append(f"title: {title}")
    if dept_expanded: parts.append(f"department: {dept_expanded}")
    if country:       parts.append(f"country: {country}")
    if purpose:       parts.append(f"purpose: {purpose}")

    return " | ".join(parts)[:8000]

# ========================== Récupération des données ==========================
def get_all_automations():
    all_processes = []
    page = 0
    total_pages = None
    while True:
        url = f"{UIPATH_BASE_URL}?page={page}"
        try:
            r = sess.get(url, headers=HEADERS_UIPATH, timeout=(5, 60))
            if r.status_code != 200:
                print(f"❌ Erreur page {page} : {r.status_code} {r.text[:300]}")
                break
            data = r.json().get("data", {})
            processes = data.get("processes", [])
            total_pages = data.get("totalPages", 0)
            print(f"✅ Page {page + 1}/{total_pages} récupérée ({len(processes)} items)")
            all_processes.extend(processes)
            page += 1
            if total_pages and page >= total_pages:
                break
        except requests.RequestException as e:
            print(f"❌ Exception réseau UiPath (page {page}) : {e}")
            break
    return all_processes

def parse_automation(item: dict) -> dict:
    country = item.get("categories", [{}])[0].get("category_name", "") if item.get("categories") else "NC"
    collaborators = ", ".join(
        f"{c.get('user_first_name', '')} {c.get('user_last_name', '')}".strip()
        for c in item.get("collaborators", [])
    )
    submitter = item.get("process_submitter", {}) or {}
    submitter_name = f"{submitter.get('user_first_name', '')} {submitter.get('user_last_name', '')}".strip()
    purpose = extract_draftjs_text(item.get("ovr-purpose", ""))

    return {
        "process_id": item.get("process_id"),
        "Country": country,
        "Departments": item.get("user_department", ""),
        "Subject Name Automation hub": item.get("process_name", ""),
        "Purpose": purpose,
        "Benefit per company (€/per year)": item.get("q2-benefit_year_kpi__display", ""),
        "Benefit per company (hours saved/year)": item.get("process_estimated_benefit_score__display", ""),
        "Oneshot Benefit": item.get("q2-oneshot_bot_profit__display", ""),
        "Automation Hub Documentation": item.get("q1-cr_documentation__display", ""),
        "PHASE": item.get("phase_name", ""),
        "Status": item.get("phase_status_name", ""),
        "Is it a local process or could it be deployed to all subs ?": item.get("q1-local_or_groups__display", ""),
        "Date submitted": item.get("process_created_epoch__display", ""),
        "Link Automation Hub": f"https://cloud.uipath.com/lyrecomanagement/DefaultTenant/automationhub_/automation-profile/{item.get('process_slug', '')}",
        "Submitter": submitter_name,
        "Collaborator": collaborators
    }

def should_keep(row: dict) -> bool:
    """Applique les exclusions métiers définies (si listes non vides)."""
    return (row.get("PHASE") not in EXCLUDED_PHASES) and (row.get("Status") not in EXCLUDED_STATUSES)

# =================== Embeddings Mistral ===================
def generate_embeddings_batch(texts, batch_size=64):
    """Embeddings par lots (plus rapide/robuste)."""
    out = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i+batch_size]
        try:
            r = sess.post(
                EMBEDDING_API_URL,
                headers={
                    "Authorization": f"Bearer {MISTRAL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"input": chunk, "model": "mistral-embed"},
                timeout=(5, 60)
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            out.extend([d["embedding"] for d in data])
            time.sleep(0.2)  # politesse API
        except requests.RequestException as e:
            print(f"❌ Exception embedding (batch {i//batch_size}) : {e}")
            out.extend([None] * len(chunk))
    return out

# ========================== Base de données / pgvector ==========================
def init_db(engine, dim: int):
    metadata = MetaData()
    table = Table(
        "automationhub", metadata,
        Column("process_id", String, primary_key=True),
        Column("Country", Text),
        Column("Departments", Text),
        Column("Subject Name Automation hub", Text),
        Column("Purpose", Text),
        Column("Benefit per company (€/per year)", Text),
        Column("Benefit per company (hours saved/year)", Text),
        Column("Oneshot Benefit", Text),
        Column("Automation Hub Documentation", Text),
        Column("PHASE", Text),
        Column("Status", Text),
        Column("Is it a local process or could it be deployed to all subs ?", Text),
        Column("Date submitted", Text),
        Column("Link Automation Hub", Text),
        Column("Submitter", Text),
        Column("Collaborator", Text),
        Column("embedding_text", Text),   # debug/traçabilité (optionnel)
        Column("embedding", Vector(dim)),
    )
    with engine.begin() as conn:
        # Extension pgvector
        conn.execute(sqltext("CREATE EXTENSION IF NOT EXISTS vector"))
        # Drop & create table
        table.drop(conn, checkfirst=True)
        table.create(conn)
        # Index IVFFlat (accélère la similarité)
        conn.execute(sqltext(
            "CREATE INDEX IF NOT EXISTS idx_automationhub_embedding "
            "ON automationhub USING ivfflat (embedding vector_l2_ops) WITH (lists = 100)"
        ))
    return table, metadata

# ========= Main =========
def main():
    # 1) Récupération UiPath
    automations = get_all_automations()
    if not automations:
        print("⚠️ Aucune automation récupérée.")
        return

    # 2) Parsing + filtres
    parsed = [parse_automation(a) for a in automations]
    df = pd.DataFrame(parsed).drop_duplicates(subset=["process_id"])
    if df.empty:
        print("⚠️ Aucune ligne après parsing.")
        return

    df = df[df.apply(lambda r: should_keep(r.to_dict()), axis=1)]
    if df.empty:
        print("⚠️ Aucune ligne après filtres métiers.")
        return

    # 3) Texte pour embeddings (enrichi)
    texts = [build_embedding_text(row) for row in df.to_dict(orient="records")]
    for i, t in enumerate(texts):
        if not t:
            title = _norm(df.iloc[i]["Subject Name Automation hub"] or "")
            texts[i] = clean_text(f"title: {title}") or "N/A"

    # 4) Embeddings (batch)
    embeddings = generate_embeddings_batch(texts, batch_size=64)
    df["embedding"] = embeddings
    df["embedding_text"] = texts
    df = df[df["embedding"].notnull()]
    if df.empty:
        print("⚠️ Aucune ligne avec embedding valide.")
        return

    # Dimension dynamique
    dim = len(df["embedding"].iloc[0])
    print(f"✅ Dimension d’embedding détectée : {dim}")

    # 5) Insertion PG (pgvector)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
    table, _ = init_db(engine, dim)

    rows = df.to_dict(orient="records")
    try:
        with engine.begin() as conn:
            conn.execute(table.insert(), rows)
        print(f"✅ {len(rows)} lignes insérées dans PostgreSQL (table automationhub).")
    except SQLAlchemyError as e:
        print("❌ Erreur d’insertion :", e)

if __name__ == "__main__":
    main()
