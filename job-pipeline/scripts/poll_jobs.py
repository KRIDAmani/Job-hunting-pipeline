#!/usr/bin/env python3
"""
poll_jobs.py — Pipeline de veille emploi d'Amani (Phase 0 + 1 sources multiples).

Sources couvertes :
  - France Travail (API officielle)      -> couverture complète à chaque run
  - Adzuna (agrège Indeed & co.)         -> rotation pour respecter le quota gratuit
  - Google Programmable Search           -> WTTJ, HelloWork, LinkedIn, JobTeaser,
                                             exécuté une fois par heure seulement

Ce que fait ce script, dans l'ordre :
  1. Interroge chaque source activée (selon les secrets disponibles)
  2. Normalise les résultats dans un format commun
  3. Filtre les offres déjà vues (dédoublonnage via data/jobs_seen.json)
  4. Filtre les offres qui matchent une exclusion (séniorité, mots-clés)
  5. Envoie un email (une notif groupée) via SMTP Gmail
  6. Met à jour data/jobs_seen.json et data/rotation_state.json
     (committés par le workflow GitHub Actions)

Toutes les clés/mots de passe sont lus depuis des variables d'environnement
(injectées via GitHub Secrets en production, ou un .env local pour tester).
Les sources Adzuna et Google Search sont OPTIONNELLES : si leurs identifiants
ne sont pas définis, elles sont simplement ignorées (pas d'erreur).
"""

import itertools
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "keywords.yaml"
SEEN_PATH = ROOT / "data" / "jobs_seen.json"
ROTATION_PATH = ROOT / "data" / "rotation_state.json"
PREAMBLE_PATH = ROOT / "templates" / "cv_preamble.tex"
MASTER_BODY_PATH = ROOT / "templates" / "cv_master_body.tex"

FT_AUTH_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
FT_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
ADZUNA_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/fr/search/1"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"

# Une offre en dessous de cette longueur de description n'a pas assez de
# matière pour justifier un CV sur-mesure (ex. offres via alertes email,
# qui n'apportent qu'un lien sans texte).
MIN_DESCRIPTION_LENGTH = 200


# ============================================================
# Utilitaires génériques
# ============================================================

def get_env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        print(f"[ERREUR] Variable d'environnement manquante : {name}", file=sys.stderr)
        sys.exit(1)
    return val


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_top_of_hour() -> bool:
    """Vrai uniquement sur le run déclenché à xx:00 (cron */30 -> une fois/heure)."""
    return datetime.now(timezone.utc).minute < 30


# ============================================================
# Source 1 — France Travail (couverture complète à chaque run)
# ============================================================

def get_ft_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        FT_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "api_offresdemploiv2 o2dsoffre",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[ERREUR AUTH FT] HTTP {resp.status_code} — réponse : {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_france_travail(config: dict) -> list[dict]:
    client_id = os.environ.get("FT_CLIENT_ID", "")
    client_secret = os.environ.get("FT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("[FT] Identifiants absents — source ignorée.")
        return []

    token = get_ft_token(client_id, client_secret)
    types_contrat = config.get("types_contrat", [])
    results = []

    for poste, zone in itertools.product(config["postes"], config["zones"]):
        params = {
            "motsCles": poste,
            "distance": zone.get("rayon_km") or 100,
            "typeContrat": ",".join(types_contrat) if types_contrat else None,
            "sort": "1",
        }
        if not zone.get("national"):
            params["departement"] = zone.get("departement")
        params = {k: v for k, v in params.items() if v is not None}

        resp = requests.get(
            FT_SEARCH_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if resp.status_code not in (200, 206):
            if resp.status_code != 204:  # 204 = aucun résultat, pas une erreur
                print(f"[FT AVERTISSEMENT] '{poste}' / {zone['nom']} -> HTTP {resp.status_code}")
            continue

        for offer in resp.json().get("resultats", []):
            results.append(normalize_ft(offer, zone["nom"]))

    return results


def normalize_ft(offer: dict, zone_nom: str) -> dict:
    return {
        "source": "France Travail",
        "id": f"ft:{offer.get('id')}",
        "titre": offer.get("intitule", ""),
        "entreprise": offer.get("entreprise", {}).get("nom", "Non précisé"),
        "lieu": offer.get("lieuTravail", {}).get("libelle", zone_nom),
        "salaire": offer.get("salaire", {}).get("libelle", ""),
        "url": offer.get("origineOffre", {}).get("urlOrigine", "N/A"),
        "description": offer.get("description", ""),
    }


# ============================================================
# Source 2 — Adzuna (rotation pour respecter le quota gratuit)
# ============================================================

def fetch_adzuna(config: dict, rotation: dict) -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        print("[ADZUNA] Identifiants absents — source ignorée.")
        return []
    if not is_top_of_hour():
        # 1 appel/30min = 1440/mois, au-dessus du quota gratuit (~1000/mois).
        # Limité à 1x/heure -> 720/mois, rotation complète en ~2 jours.
        print("[ADZUNA] Pas le run horaire pivot — source sautée ce coup-ci.")
        return []

    combos = list(itertools.product(config["postes"], config["zones"]))
    if not combos:
        return []

    # On ne traite qu'UNE combinaison par run (quota gratuit ~1000/mois),
    # et on reprend là où on s'était arrêté au run précédent.
    idx = rotation.get("adzuna_index", 0) % len(combos)
    poste, zone = combos[idx]
    rotation["adzuna_index"] = (idx + 1) % len(combos)

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": poste,
        "results_per_page": 20,
        "sort_by": "date",
    }
    if not zone.get("national"):
        params["where"] = zone["nom"]

    resp = requests.get(ADZUNA_SEARCH_URL, params=params, timeout=20)
    if resp.status_code != 200:
        print(f"[ADZUNA AVERTISSEMENT] '{poste}' / {zone['nom']} -> HTTP {resp.status_code} — {resp.text[:200]}")
        return []

    print(f"[ADZUNA] Combinaison traitée : '{poste}' / {zone['nom']} ({idx + 1}/{len(combos)})")
    return [normalize_adzuna(job) for job in resp.json().get("results", [])]


def normalize_adzuna(job: dict) -> dict:
    return {
        "source": "Adzuna",
        "id": f"adzuna:{job.get('id')}",
        "titre": job.get("title", ""),
        "entreprise": job.get("company", {}).get("display_name", "Non précisé"),
        "lieu": job.get("location", {}).get("display_name", "Non précisé"),
        "salaire": _format_adzuna_salary(job),
        "url": job.get("redirect_url", "N/A"),
        "description": job.get("description", ""),
    }


def _format_adzuna_salary(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"{int(lo):,} - {int(hi):,} €".replace(",", " ")
    return ""


# ============================================================
# Source 3 — Alertes email natives (LinkedIn, WTTJ, HelloWork,
# JobTeaser) lues via IMAP. Remplace Google Programmable Search,
# fermé aux nouveaux comptes depuis 2025.
#
# Prérequis côté Gmail (voir README) : un filtre qui applique le
# libellé "Veille-Alertes" aux emails provenant de ces 4 plateformes.
# ============================================================

IMAP_LABEL = "Veille-Alertes"

# Un motif d'URL par domaine, pour extraire les liens d'offres
# au milieu du HTML/texte de l'email (les alertes contiennent
# souvent des dizaines de liens de tracking en plus du lien réel).
LINK_PATTERNS = {
    "LinkedIn": re.compile(r"https://[a-z.]*linkedin\.com/jobs/view/[^\s\"'<>]+", re.IGNORECASE),
    "Welcome to the Jungle": re.compile(r"https://www\.welcometothejungle\.com/fr/companies/[^\s\"'<>]+/jobs/[^\s\"'<>]+", re.IGNORECASE),
    "HelloWork": re.compile(r"https://www\.hellowork\.com/fr-fr/emplois/[^\s\"'<>]+", re.IGNORECASE),
    "JobTeaser": re.compile(r"https://www\.jobteaser\.com/fr/job-offers/[^\s\"'<>]+", re.IGNORECASE),
    "Indeed": re.compile(r"https://[a-z.]*indeed\.com/(?:viewjob|rc/clk)[^\s\"'<>]+", re.IGNORECASE),
}


def fetch_email_alerts() -> list[dict]:
    import email
    import imaplib

    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_address or not gmail_app_password:
        print("[ALERTES EMAIL] Identifiants Gmail absents — source ignorée.")
        return []

    results = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(gmail_address, gmail_app_password)
        status, _ = imap.select(f'"{IMAP_LABEL}"')
        if status != "OK":
            print(f"[ALERTES EMAIL] Libellé '{IMAP_LABEL}' introuvable — vérifie le filtre Gmail (voir README).")
            imap.logout()
            return []

        status, data = imap.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            imap.logout()
            return []

        msg_ids = data[0].split()
        print(f"[ALERTES EMAIL] {len(msg_ids)} email(s) non lu(s) à traiter.")

        for msg_id in msg_ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = msg.get("From", "")
            body = _extract_email_body(msg)

            for site_nom, pattern in LINK_PATTERNS.items():
                for url in set(pattern.findall(body)):
                    results.append({
                        "source": f"Alerte email ({site_nom})",
                        "id": f"emailalert:{url}",
                        "titre": f"Offre trouvée via alerte {site_nom}",
                        "entreprise": "Voir l'annonce",
                        "lieu": "Voir l'annonce",
                        "salaire": "",
                        "url": url,
                        "description": "",
                    })
        imap.logout()
    except Exception as exc:  # noqa: BLE001 — on ne veut jamais planter tout le run pour ça
        print(f"[ALERTES EMAIL AVERTISSEMENT] {exc}")

    return results


# ============================================================
# Source — Entreprises ciblées via leur ATS (Greenhouse/Lever/Ashby)
# Couverture complète à chaque run : API publiques, pas de quota.
# ============================================================

def fetch_ats_boards(config: dict) -> list[dict]:
    postes_lower = [p.lower() for p in config.get("postes", [])]
    results = []

    for entreprise in config.get("entreprises_ats", []):
        plateforme = entreprise.get("plateforme")
        slug = entreprise.get("slug")
        nom = entreprise.get("nom", slug)

        try:
            if plateforme == "greenhouse":
                jobs = _fetch_greenhouse(slug)
            elif plateforme == "lever":
                jobs = _fetch_lever(slug)
            elif plateforme == "ashby":
                jobs = _fetch_ashby(slug)
            else:
                print(f"[ATS AVERTISSEMENT] Plateforme inconnue pour {nom} : '{plateforme}'")
                continue
        except requests.RequestException as exc:
            print(f"[ATS AVERTISSEMENT] {nom} ({plateforme}) -> {exc}")
            continue

        for job in jobs:
            titre = job.get("titre", "")
            # Ces API renvoient TOUTES les offres de l'entreprise —
            # on ne garde que celles qui matchent un de tes postes.
            if not any(p in titre.lower() for p in postes_lower):
                continue
            job["entreprise"] = nom
            results.append(job)

    return results


def _fetch_greenhouse(slug: str) -> list[dict]:
    resp = requests.get(GREENHOUSE_API.format(slug=slug), timeout=20)
    if resp.status_code != 200:
        return []
    return [
        {
            "source": "Greenhouse",
            "id": f"greenhouse:{j.get('id')}",
            "titre": j.get("title", ""),
            "entreprise": "",
            "lieu": j.get("location", {}).get("name", "Non précisé"),
            "salaire": "",
            "url": j.get("absolute_url", "N/A"),
            "description": j.get("content", "") or "",
        }
        for j in resp.json().get("jobs", [])
    ]


def _fetch_lever(slug: str) -> list[dict]:
    resp = requests.get(LEVER_API.format(slug=slug), timeout=20)
    if resp.status_code != 200:
        return []
    return [
        {
            "source": "Lever",
            "id": f"lever:{j.get('id')}",
            "titre": j.get("text", ""),
            "entreprise": "",
            "lieu": (j.get("categories") or {}).get("location", "Non précisé"),
            "salaire": "",
            "url": j.get("hostedUrl", "N/A"),
            "description": j.get("descriptionPlain", "") or "",
        }
        for j in resp.json()
    ]


def _fetch_ashby(slug: str) -> list[dict]:
    resp = requests.get(ASHBY_API.format(slug=slug), timeout=20)
    if resp.status_code != 200:
        return []
    return [
        {
            "source": "Ashby",
            "id": f"ashby:{j.get('id')}",
            "titre": j.get("title", ""),
            "entreprise": "",
            "lieu": j.get("location", "Non précisé"),
            "salaire": "",
            "url": j.get("jobUrl", "N/A"),
            "description": j.get("descriptionPlain", "") or "",
        }
        for j in resp.json().get("jobs", [])
    ]

def _extract_email_body(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html"):
                try:
                    parts.append(part.get_payload(decode=True).decode(errors="ignore"))
                except Exception:  # noqa: BLE001
                    continue
        return "\n".join(parts)
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


# ============================================================
# Génération de CV sur-mesure (Claude API + compilation LaTeX)
# ============================================================

CV_SYSTEM_PROMPT = """Tu adaptes un CV LaTeX existant à une offre d'emploi précise.

RÈGLES ABSOLUES, NON NÉGOCIABLES :
1. N'INVENTE JAMAIS une compétence, un outil, une expérience, une techno
   ou un résultat qui n'apparaît PAS déjà dans le corps de CV fourni.
   Si l'offre demande une techno absente du CV, tu ne l'ajoutes pas.
2. Tu ne modifies JAMAIS les faits : dates, intitulés d'expériences,
   entreprises, diplômes, chiffres/métriques (28%, 12%, 40x, 0,847, etc.)
   doivent rester strictement identiques.
3. Ce que tu PEUX faire :
   - Réordonner les puces (\\resumeItem) au sein d'une expérience/projet
     pour mettre en avant celles les plus pertinentes pour l'offre.
   - Réordonner les outils listés dans un \\tools{{...}} (jamais en ajouter
     ou en retirer).
   - Réordonner les catégories/items de la section COMPÉTENCES.
   - Réécrire la phrase cible du paragraphe PROFIL (ex. "je recherche un
     poste en X") pour refléter l'intitulé/l'entreprise de l'offre, en
     gardant intacts les faits (BAC+6, mobilité, ville de résidence).
4. Ne touche à AUCUNE commande de mise en forme, aucun \\section, aucune
   structure — uniquement le contenu textuel et l'ordre des éléments.
5. Respecte scrupuleusement la syntaxe LaTeX : échappe les caractères
   spéciaux (%, &, _, #) avec un backslash si tu les introduis.

FORMAT DE RÉPONSE :
Réponds UNIQUEMENT avec le LaTeX complet, en commençant exactement par
"\\begin{{document}}" et en terminant exactement par "\\end{{document}}".
Aucun texte avant, aucun texte après, aucun bloc de code markdown."""


def call_claude_for_cv(offer: dict, master_body: str) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    user_message = f"""Voici le corps de CV de référence (LaTeX complet, richesse de mots-clés à préserver) :

{master_body}

---

Offre à cibler :
Poste : {offer.get('titre')}
Entreprise : {offer.get('entreprise')}
Description : {offer.get('description', '')[:4000]}

Adapte le CV selon les règles définies dans le system prompt."""

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "system": CV_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"[CV AVERTISSEMENT] Appel Claude échoué -> HTTP {resp.status_code} — {resp.text[:300]}")
        return None

    content_blocks = resp.json().get("content", [])
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()

    # Filet de sécurité si le modèle a quand même entouré la réponse de ```
    text = re.sub(r"^```(?:latex)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    if not text.startswith(r"\begin{document}") or not text.endswith(r"\end{document}"):
        print("[CV AVERTISSEMENT] Réponse de Claude hors format attendu — CV non généré pour cette offre.")
        return None

    return text


def compile_latex_to_pdf(tex_source: str, workdir: Path) -> Path | None:
    tex_path = workdir / "cv.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    try:
        result = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "cv.tex"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[CV AVERTISSEMENT] Compilation LaTeX impossible : {exc}")
        return None

    pdf_path = workdir / "cv.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        print(f"[CV AVERTISSEMENT] Échec de compilation LaTeX (code {result.returncode}) — CV non envoyé pour cette offre.")
        print(result.stdout[-1500:])
        return None

    return pdf_path


def send_cv_draft(offer: dict, pdf_path: Path) -> None:
    gmail_address = get_env("GMAIL_ADDRESS")
    gmail_app_password = get_env("GMAIL_APP_PASSWORD")
    recipient = get_env("RECIPIENT_EMAIL")

    entreprise = re.sub(r"[^a-zA-Z0-9]+", "-", offer.get("entreprise", "offre")).strip("-")[:40]

    msg = MIMEMultipart()
    msg["Subject"] = f"[BROUILLON À VALIDER] CV pour {offer.get('titre')} — {offer.get('entreprise')}"
    msg["From"] = gmail_address
    msg["To"] = recipient

    body = (
        f"CV généré automatiquement pour cette offre — À RELIRE avant tout envoi.\n\n"
        f"Poste : {offer.get('titre')}\n"
        f"Entreprise : {offer.get('entreprise')}\n"
        f"Lien de l'offre : {offer.get('url')}\n\n"
        f"Rappel : ce CV réordonne et met en avant des éléments déjà présents dans "
        f"ton CV maître — rien n'a été inventé, mais relis-le avant de l'envoyer."
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=f"CV_AmaniKRID_{entreprise}.pdf")
    msg.attach(attachment)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [recipient], msg.as_string())


def generate_and_send_cv(offer: dict, master_body: str) -> None:
    description = offer.get("description", "")
    if len(description) < MIN_DESCRIPTION_LENGTH:
        return  # pas assez de matière (ex. offre remontée par alerte email)

    tailored_body = call_claude_for_cv(offer, master_body)
    if not tailored_body:
        return

    preamble = PREAMBLE_PATH.read_text(encoding="utf-8")
    full_tex = preamble + "\n" + tailored_body

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = compile_latex_to_pdf(full_tex, Path(tmp))
        if not pdf_path:
            return
        send_cv_draft(offer, pdf_path)
        print(f"[CV] Brouillon envoyé pour : {offer.get('titre')} — {offer.get('entreprise')}")




def extract_years_required(text: str) -> int | None:
    match = re.search(r"(\d{1,2})\s*(?:\+)?\s*ans?\s+d.?exp[eé]rience", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_excluded(offer: dict, config: dict) -> str | None:
    full_text = f"{offer.get('titre', '')} {offer.get('description', '')}"

    for pattern in config.get("exclusions_texte", []):
        if re.search(pattern, full_text, re.IGNORECASE):
            return f"motif exclu : {pattern}"

    seuil = config.get("seuil_max_annees_experience")
    if seuil:
        years = extract_years_required(full_text)
        if years and years > seuil:
            return f"séniorité trop élevée : {years} ans requis (seuil {seuil})"

    return None


def build_email_body(new_offers: list[dict], config: dict) -> str:
    lines = [f"{len(new_offers)} nouvelle(s) offre(s) correspondant à tes critères :\n"]
    plancher = config.get("salaire_plancher_brut_annuel")
    for offer in new_offers:
        lines.append(f"— [{offer['source']}] {offer['titre']}")
        lines.append(f"  Entreprise : {offer['entreprise']}")
        lines.append(f"  Lieu : {offer['lieu']}")
        if offer.get("salaire"):
            lines.append(f"  Salaire indiqué : {offer['salaire']}")
        lines.append(f"  Lien : {offer['url']}")
        lines.append("")
    if plancher:
        lines.append(f"(Rappel : plancher légal Passeport Talent = {plancher} € brut/an)")
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    gmail_address = get_env("GMAIL_ADDRESS")
    gmail_app_password = get_env("GMAIL_APP_PASSWORD")
    recipient = get_env("RECIPIENT_EMAIL")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [recipient], msg.as_string())


# ============================================================
# Orchestration
# ============================================================

def main() -> None:
    config = load_config()
    seen = set(load_json(SEEN_PATH, []))
    rotation = load_json(ROTATION_PATH, {})

    all_offers = []
    all_offers += fetch_france_travail(config)
    all_offers += fetch_adzuna(config, rotation)
    all_offers += fetch_ats_boards(config)
    all_offers += fetch_email_alerts()

    new_offers = []
    excluded_count = 0

    for offer in all_offers:
        offer_id = offer.get("id")
        if not offer_id or offer_id in seen:
            continue
        seen.add(offer_id)

        reason = is_excluded(offer, config)
        if reason:
            excluded_count += 1
            continue

        new_offers.append(offer)

    print(f"Offres neuves retenues : {len(new_offers)} | exclues : {excluded_count} | total inspecté : {len(all_offers)}")

    if new_offers:
        subject = f"[Veille emploi] {len(new_offers)} nouvelle(s) offre(s)"
        body = build_email_body(new_offers, config)
        send_email(subject, body)
        print("Email envoyé.")

        if os.environ.get("ANTHROPIC_API_KEY"):
            master_body = MASTER_BODY_PATH.read_text(encoding="utf-8")
            for offer in new_offers:
                generate_and_send_cv(offer, master_body)
        else:
            print("[CV] ANTHROPIC_API_KEY absent — génération de CV désactivée.")
    else:
        print("Aucune nouvelle offre retenue — pas d'email envoyé.")

    save_json(SEEN_PATH, sorted(seen))
    save_json(ROTATION_PATH, rotation)


if __name__ == "__main__":
    main()
