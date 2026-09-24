"""
migrate_tenant.py — Migration de cartes AutomationHub (ancien tenant → nouveau tenant)

Ce que fait ce script :
  1. Lit toutes les cartes de l'ancien tenant via l'API OpenAPI
  2. Pour chaque carte, génère un nouveau purpose structuré via Mistral AI
     (Contexte / Objectif / Bénéfices attendus / Type de déploiement)
  3. Remet la carte en phase initiale (INITIAL_PHASE_ID) sur le nouveau tenant
  4. Remappe les emails des collaborateurs (OLD_EMAIL_DOMAIN → NEW_EMAIL_DOMAIN)
  5. Résout les catégories par nom (les IDs diffèrent entre tenants)
  6. Télécharge les documents de l'ancienne carte et les uploade sur la nouvelle

Usage :
    python migrate_tenant.py [--dry-run] [--submitters "Alice Doe,Bob Smith"] [--phases "In Review,Approved"]

Variables .env requises :
    OLD_TENANT_TOKEN         Bearer token de l'ancien tenant
    OLD_TENANT_BASE_URL      https://cloud.uipath.com/<org>/<ancien>/automationhub_/api/v1/openapi/automations
    NEW_TENANT_TOKEN         Bearer token du nouveau tenant
    NEW_TENANT_BASE_URL      https://cloud.uipath.com/<org>/<nouveau>/automationhub_/api/v1/openapi/automations
    MISTRAL_API_KEY          Clé API Mistral (génération du purpose)
    OLD_EMAIL_DOMAIN         ex: lyreco.com
    NEW_EMAIL_DOMAIN         ex: lyrecomanagement.com
    INITIAL_PHASE_ID         ID de la phase initiale sur le nouveau tenant (défaut: 1)
    PROXY_URL                (optionnel) http://proxy.lyreco.com:8080
    REQUESTS_CA_BUNDLE       (optionnel) chemin vers le certificat corporate
"""

import os
import sys
import json
import time
import argparse
import mimetypes
import logging
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

OLD_TOKEN        = os.getenv("OLD_TENANT_TOKEN")
OLD_BASE_URL     = os.getenv("OLD_TENANT_BASE_URL")
NEW_TOKEN        = os.getenv("NEW_TENANT_TOKEN")
NEW_BASE_URL     = os.getenv("NEW_TENANT_BASE_URL")
MISTRAL_API_KEY  = os.getenv("MISTRAL_API_KEY")
OLD_EMAIL_DOMAIN = os.getenv("OLD_EMAIL_DOMAIN", "")
NEW_EMAIL_DOMAIN = os.getenv("NEW_EMAIL_DOMAIN", "")
INITIAL_PHASE_ID = int(os.getenv("INITIAL_PHASE_ID", "1"))
PROXY_URL        = os.getenv("PROXY_URL")
CA_BUNDLE        = os.getenv("REQUESTS_CA_BUNDLE")

REQUIRED = {
    "OLD_TENANT_TOKEN": OLD_TOKEN, "OLD_TENANT_BASE_URL": OLD_BASE_URL,
    "NEW_TENANT_TOKEN": NEW_TOKEN, "NEW_TENANT_BASE_URL": NEW_BASE_URL,
    "MISTRAL_API_KEY": MISTRAL_API_KEY,
}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    sys.exit(f"❌  Variables manquantes dans .env : {', '.join(missing)}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("migration.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Session HTTP ─────────────────────────────────────────────────────────────

def _make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": token,
        "x-ah-openapi-auth": "openapi-token",
        "Content-Type": "application/json",
    })
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    if CA_BUNDLE:
        s.verify = CA_BUNDLE
    retry = Retry(total=5, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

old_sess = _make_session(OLD_TOKEN)
new_sess = _make_session(NEW_TOKEN)

def _root(url: str) -> str:
    return url.rstrip("/").rsplit("/openapi/automations", 1)[0]

OLD_ROOT = _root(OLD_BASE_URL)
NEW_ROOT = _root(NEW_BASE_URL)

# ─── Helpers DraftJS ──────────────────────────────────────────────────────────

def text_to_draftjs(text: str) -> str:
    """Convertit du texte plain (avec sauts de ligne) en JSON DraftJS."""
    if not text:
        return json.dumps({"blocks": [], "entityMap": {}})
    blocks = []
    for i, line in enumerate(text.splitlines()):
        blocks.append({
            "key": f"b{i}",
            "text": line,
            "type": "unstyled",
            "depth": 0,
            "inlineStyleRanges": [],
            "entityRanges": [],
        })
    return json.dumps({"blocks": blocks, "entityMap": {}})


def extract_draftjs_text(raw: str) -> str:
    if not raw:
        return ""
    try:
        d = json.loads(raw)
        return "\n".join(b.get("text", "") for b in d.get("blocks", []))
    except Exception:
        return raw or ""

# ─── Génération du purpose via Mistral ───────────────────────────────────────

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"

_PROMPT_SYSTEM = (
    "Tu es un expert en automatisation RPA et gestion de processus. "
    "Tu rédiges des descriptions de projets d'automatisation claires, professionnelles et précises en français. "
    "Sois concis (maximum 250 mots). Ne répète pas le nom du processus dans chaque section."
)

_PROMPT_TEMPLATE = """\
Rédige une description structurée pour la carte AutomationHub suivante :

Nom du processus : {name}
Département      : {department}
Pays / périmètre : {country}
Bénéfices (€/an) : {benefit_eur}
Bénéfices (h/an) : {benefit_hours}
Déploiement      : {deployment}
Description existante (brut, peut être vide ou incomplète) :
{existing_purpose}

Génère exactement ces 4 sections (une ligne vide entre chaque) :
## Contexte
<décris le processus actuel, ses contraintes et son contexte métier>

## Objectif
<décris ce que l'automatisation va faire et comment>

## Bénéfices attendus
<liste les gains concrets : temps, coût, qualité, fréquence>

## Type de déploiement
<local / groupe / global + justification courte>
"""


def generate_purpose(detail: dict) -> str:
    """
    Appelle Mistral pour générer un purpose structuré.
    Retourne le texte brut (sera converti en DraftJS ensuite).
    En cas d'échec API, retourne le purpose existant tel quel.
    """
    existing_raw = detail.get("ovr-purpose", "")
    existing_text = extract_draftjs_text(existing_raw)

    prompt = _PROMPT_TEMPLATE.format(
        name=detail.get("process_name", ""),
        department=detail.get("user_department", "N/A"),
        country=(detail.get("categories") or [{}])[0].get("category_name", "N/A"),
        benefit_eur=detail.get("q2-benefit_year_kpi__display", "N/A"),
        benefit_hours=detail.get("process_estimated_benefit_score__display", "N/A"),
        deployment=detail.get("q1-local_or_groups__display", "N/A"),
        existing_purpose=existing_text[:600] if existing_text else "(aucune description existante)",
    )

    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    try:
        r = requests.post(
            MISTRAL_CHAT_URL,
            headers=headers,
            json={
                "model": "mistral-small-latest",
                "messages": [
                    {"role": "system", "content": _PROMPT_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
            },
            proxies=proxies,
            verify=CA_BUNDLE or True,
            timeout=(10, 60),
        )
        r.raise_for_status()
        generated = r.json()["choices"][0]["message"]["content"].strip()
        log.info("  🤖 Purpose généré (%d car.)", len(generated))
        return generated
    except Exception as e:
        log.warning("  ⚠️  Mistral indisponible (%s) — purpose existant conservé", e)
        return existing_text

# ─── Résolution des catégories (nouveau tenant) ───────────────────────────────

_new_categories_cache: dict[str, int] | None = None


def _load_new_categories() -> dict[str, int]:
    """Charge les catégories du nouveau tenant une seule fois (nom → id)."""
    global _new_categories_cache
    if _new_categories_cache is not None:
        return _new_categories_cache

    url = f"{NEW_ROOT}/openapi/categories"
    r = new_sess.get(url, timeout=(10, 30))
    if r.status_code != 200:
        log.warning("Impossible de charger les catégories du nouveau tenant : %d", r.status_code)
        _new_categories_cache = {}
        return _new_categories_cache

    cats = r.json().get("data", {}).get("categories", [])
    _new_categories_cache = {
        c.get("category_name", "").strip().lower(): c.get("category_id") or c.get("id")
        for c in cats
        if c.get("category_name")
    }
    log.info("  📂 %d catégories chargées depuis le nouveau tenant", len(_new_categories_cache))
    return _new_categories_cache


def remap_categories(old_categories: list[dict]) -> list[dict]:
    """
    Traduit les catégories de l'ancien tenant vers les IDs du nouveau.
    Si une catégorie n'existe pas sur le nouveau tenant, elle est ignorée avec un warning.
    """
    mapping = _load_new_categories()
    remapped = []
    for cat in old_categories:
        name = (cat.get("category_name") or "").strip()
        new_id = mapping.get(name.lower())
        if new_id:
            remapped.append({"category_id": new_id, "category_name": name})
        else:
            log.warning("  ⚠️  Catégorie '%s' absente du nouveau tenant — ignorée", name)
    return remapped

# ─── Remapping des emails ─────────────────────────────────────────────────────

def remap_email(email: str) -> str:
    """Remplace l'ancien domaine email par le nouveau."""
    if OLD_EMAIL_DOMAIN and NEW_EMAIL_DOMAIN and email.endswith(f"@{OLD_EMAIL_DOMAIN}"):
        return email.replace(f"@{OLD_EMAIL_DOMAIN}", f"@{NEW_EMAIL_DOMAIN}")
    return email

# ─── Lecture ancien tenant ────────────────────────────────────────────────────

def fetch_all_automations() -> list[dict]:
    all_items, page = [], 0
    while True:
        url = f"{OLD_BASE_URL}?page={page}&perpage=50&status=all"
        r = old_sess.get(url, timeout=(10, 90))
        if r.status_code != 200:
            log.error("Erreur GET page %d : %d %s", page, r.status_code, r.text[:200])
            break
        data  = r.json().get("data", {})
        items = data.get("processes", [])
        total = data.get("totalPages", 0)
        log.info("Page %d/%d — %d cartes", page + 1, total, len(items))
        all_items.extend(items)
        page += 1
        if total and page >= total:
            break
        time.sleep(0.3)
    return all_items


def fetch_card_detail(process_id: int) -> dict:
    r = old_sess.get(f"{OLD_BASE_URL}/{process_id}", timeout=(10, 60))
    if r.status_code == 200:
        return r.json().get("data", {})
    log.warning("Impossible de récupérer le détail de la carte %s : %d", process_id, r.status_code)
    return {}


def fetch_documents(process_id: int) -> list[dict]:
    for path in (
        f"{OLD_ROOT}/openapi/automations/{process_id}/documents",
        f"{OLD_ROOT}/aom/automation/{process_id}/media",
    ):
        r = old_sess.get(path, timeout=(10, 60))
        if r.status_code == 200:
            body = r.json().get("data", {})
            return body.get("documents", body.get("media", []))
    log.warning("Impossible de lister les docs de la carte %s", process_id)
    return []


def download_document(process_id: int, media_id: int) -> bytes | None:
    for path in (
        f"{OLD_ROOT}/openapi/automations/{process_id}/documents/{media_id}/download",
        f"{OLD_ROOT}/aom/automation/{process_id}/media/{media_id}/download",
    ):
        r = old_sess.get(path, timeout=(15, 120), stream=True)
        if r.status_code == 200:
            return r.content
    log.warning("Téléchargement échoué doc %s (carte %s)", media_id, process_id)
    return None

# ─── Construction du payload nouveau tenant ───────────────────────────────────

_Q_PREFIXES = ("q1-", "q2-", "q3-", "q4-")

# Champs à ne jamais recopier (IDs internes, dates système, métadonnées lecture seule)
_DROP_FIELDS = frozenset({
    "process_id", "process_slug", "process_automation_id",
    "process_created_epoch", "process_created_epoch__display",
    "process_updated_epoch", "process_updated_epoch__display",
    "phase_id", "phase_name", "phase_status_id", "phase_status_name",
    "process_submitter", "ovr-purpose",
})


def build_create_payload(detail: dict, generated_purpose: str) -> dict:
    """
    Construit le payload de création pour le nouveau tenant :
    - purpose : généré par Mistral (DraftJS)
    - phase   : réinitialisée à INITIAL_PHASE_ID
    - emails  : remappés vers le nouveau domaine
    - catégories : résolues par nom sur le nouveau tenant
    - champs q1-/q2-/q3-/q4- : copiés tels quels
    """
    payload: dict = {
        "process_name":    detail.get("process_name", ""),
        "user_department": detail.get("user_department", ""),
        "phase_id":        INITIAL_PHASE_ID,

        # Purpose Mistral → DraftJS
        "ovr-purpose": text_to_draftjs(generated_purpose),

        # Catégories résolues par nom (IDs nouveaux)
        "categories": remap_categories(detail.get("categories") or []),

        # Collaborateurs avec emails remappés
        "collaborators": [
            {"user_email": remap_email(c.get("user_email", ""))}
            for c in (detail.get("collaborators") or [])
            if c.get("user_email")
        ],
    }

    # Copie des champs questionnaire (q1-/q2-/…) sans modifier leur valeur
    for key, val in detail.items():
        if any(key.startswith(p) for p in _Q_PREFIXES) and key not in _DROP_FIELDS:
            payload[key] = val

    return payload

# ─── Création sur le nouveau tenant ──────────────────────────────────────────

def create_card(payload: dict) -> int | None:
    r = new_sess.post(NEW_BASE_URL, json=payload, timeout=(10, 60))
    if r.status_code in (200, 201):
        new_id = r.json().get("data", {}).get("process_id")
        log.info("  ✅ Carte créée → nouveau ID %s", new_id)
        return new_id
    log.error("  ❌ Échec création '%s' : %d %s",
              payload.get("process_name"), r.status_code, r.text[:300])
    return None


def upload_document(new_process_id: int, filename: str, content: bytes) -> bool:
    url = f"{NEW_ROOT}/openapi/automations/{new_process_id}/documents"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {k: v for k, v in new_sess.headers.items() if k.lower() != "content-type"}
    r = requests.post(
        url, headers=headers,
        files={"file": (filename, content, mime)},
        proxies=new_sess.proxies if PROXY_URL else None,
        verify=CA_BUNDLE or True,
        timeout=(15, 120),
    )
    if r.status_code in (200, 201):
        log.info("    📎 '%s' uploadé", filename)
        return True
    log.warning("    ⚠️  Upload échoué '%s' : %d %s", filename, r.status_code, r.text[:200])
    return False

# ─── Filtres ──────────────────────────────────────────────────────────────────

def apply_filters(items: list[dict], submitters: list[str], phases: list[str]) -> list[dict]:
    filtered = []
    for item in items:
        if phases and item.get("phase_name", "") not in phases:
            continue
        if submitters:
            sub  = item.get("process_submitter") or {}
            full = f"{sub.get('user_first_name','')} {sub.get('user_last_name','')}".strip()
            collab_names = [
                f"{c.get('user_first_name','')} {c.get('user_last_name','')}".strip()
                for c in (item.get("collaborators") or [])
            ]
            if full not in submitters and not any(s in collab_names for s in submitters):
                continue
        filtered.append(item)
    return filtered

# ─── Rapport final ────────────────────────────────────────────────────────────

def print_summary(results: list[dict]):
    ok   = [r for r in results if r["status"] == "ok"]
    warn = [r for r in results if r["status"] == "partial"]
    fail = [r for r in results if r["status"] == "error"]

    log.info("─" * 60)
    log.info("RÉSUMÉ MIGRATION")
    log.info("  ✅ Succès complets   : %d", len(ok))
    log.info("  ⚠️  Partiels (docs)  : %d", len(warn))
    log.info("  ❌ Échecs            : %d", len(fail))
    if fail:
        log.info("  Cartes en échec :")
        for r in fail:
            log.info("    - [%s] %s", r["old_id"], r["name"])
    if warn:
        log.info("  Cartes avec docs non migrés :")
        for r in warn:
            log.info("    - [%s] %s  (docs échoués: %s)",
                     r["old_id"], r["name"],
                     ", ".join(str(d) for d in r.get("failed_docs", [])))
    log.info("─" * 60)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migration AutomationHub tenant-to-tenant")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyse sans rien créer sur le nouveau tenant")
    parser.add_argument("--submitters", default="",
                        help='Filtrer par soumetteur/collaborateur (ex: "Alice Doe,Bob Smith")')
    parser.add_argument("--phases", default="",
                        help='Filtrer par phase (ex: "In Review,Approved")')
    args = parser.parse_args()

    submitter_filter = [s.strip() for s in args.submitters.split(",") if s.strip()]
    phase_filter     = [p.strip() for p in args.phases.split(",") if p.strip()]

    if args.dry_run:
        log.info("🔍  MODE DRY-RUN — aucune écriture sur le nouveau tenant")

    log.info("=== Récupération des cartes (ancien tenant) ===")
    all_items = fetch_all_automations()
    log.info("Total récupéré : %d cartes", len(all_items))

    if submitter_filter or phase_filter:
        all_items = apply_filters(all_items, submitter_filter, phase_filter)
        log.info("Après filtres : %d cartes", len(all_items))

    if not all_items:
        log.warning("Aucune carte à migrer selon les filtres.")
        return

    results = []
    for idx, item in enumerate(all_items, 1):
        old_id = item.get("process_id")
        name   = item.get("process_name", "?")
        log.info("[%d/%d] '%s'  (ancien ID: %s)", idx, len(all_items), name, old_id)

        # Détail complet
        detail = fetch_card_detail(old_id)
        if not detail:
            results.append({"old_id": old_id, "name": name, "status": "error"})
            continue

        # Purpose Mistral
        generated_purpose = generate_purpose(detail)

        # Documents
        docs = fetch_documents(old_id)
        log.info("  📎 %d document(s) trouvé(s)", len(docs))

        if args.dry_run:
            log.info("  [DRY-RUN] serait créée avec purpose IA + %d doc(s)", len(docs))
            log.info("  [DRY-RUN] Aperçu purpose :\n%s", generated_purpose[:300])
            results.append({"old_id": old_id, "name": name, "status": "ok", "new_id": None})
            continue

        # Création
        payload = build_create_payload(detail, generated_purpose)
        new_id  = create_card(payload)
        if not new_id:
            results.append({"old_id": old_id, "name": name, "status": "error"})
            continue

        # Upload documents
        failed_docs = []
        for doc in docs:
            media_id = doc.get("media_id") or doc.get("id")
            filename = doc.get("media_name") or doc.get("name") or f"document_{media_id}"
            content  = download_document(old_id, media_id)
            if content is None:
                failed_docs.append(media_id)
                continue
            if not upload_document(new_id, filename, content):
                failed_docs.append(media_id)
            time.sleep(0.2)

        results.append({
            "old_id": old_id, "new_id": new_id, "name": name,
            "status": "partial" if failed_docs else "ok",
            "failed_docs": failed_docs,
        })
        time.sleep(0.5)

    print_summary(results)


if __name__ == "__main__":
    main()
