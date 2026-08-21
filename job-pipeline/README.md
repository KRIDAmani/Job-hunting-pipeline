# Pipeline de veille emploi — Multi-sources

Couvre désormais :
- **France Travail** (API officielle) — couverture complète (10 postes × 5 zones) à chaque run de 30 min.
- **Adzuna** (agrège Indeed & co.) — rotation horaire pour respecter le quota gratuit (~1000 appels/mois).
- **Alertes email natives** LinkedIn / Welcome to the Jungle / HelloWork / JobTeaser — lues automatiquement
  via IMAP avec ton compte Gmail existant (aucun nouveau compte à créer).

Zones couvertes, dans l'ordre de priorité : **Nice → Paris → Lyon → Marseille → France entière**.
Postes couverts : Data Scientist, Data Engineer, Ingénieur IA, Machine Learning Engineer, Ingénieur logiciel,
MLOps, Data Analyst, Développeur Java, Développeur C++, Test/qualification logiciel.

Exclusion inchangée : offres exigeant 10+ ans d'expérience (ou plus généralement > 7 ans, seuil ajustable
dans `config/keywords.yaml`).

> Note technique : la Google Programmable Search API (initialement prévue pour ces 4 sites) est **fermée
> aux nouveaux comptes depuis 2025** — impossible d'en créer un aujourd'hui. On utilise donc les alertes
> email natives de chaque plateforme à la place, ce qui était de toute façon notre option la plus sûre
> légalement (pas de scraping).

---

## Étape 1 — France Travail (déjà fait, rien à refaire)

## Étape 2 — Créer un compte développeur Adzuna (5 min)

1. Va sur https://developer.adzuna.com/ et inscris-toi.
2. Une fois connectée, ton tableau de bord affiche directement ton **Application ID** (`app_id`)
   et ta **Application Key** (`app_key`).
3. Ce sont `ADZUNA_APP_ID` et `ADZUNA_APP_KEY`.

## Étape 3 — Configurer les alertes email natives (15 min, une fois pour toutes)

**a) Crée les alertes sur chaque plateforme** (avec tes critères habituels — postes, localisation) :
- LinkedIn : Recherche d'emploi → Créer une alerte
- Welcome to the Jungle : sauvegarde ta recherche → active les alertes email
- HelloWork : idem, alerte email sur ta recherche
- JobTeaser : idem, dans ton espace ECL

**b) Crée un libellé Gmail dédié `Veille-Alertes`** et un filtre qui l'applique automatiquement :
1. Dans Gmail, va dans **Paramètres (roue crantée) → Voir tous les paramètres → Filtres et adresses bloquées**.
2. **Créer un filtre**.
3. Dans "De" (From), mets par exemple : `jobalerts-noreply@linkedin.com OR welcometothejungle.com OR hellowork.com OR jobteaser.com`
   (ajuste selon l'adresse exacte de l'expéditeur — regarde un email d'alerte déjà reçu pour la copier précisément).
4. Clique **Rechercher**, vérifie que les bons emails apparaissent, puis **Créer un filtre**.
5. Coche **Appliquer le libellé** → crée un nouveau libellé nommé exactement `Veille-Alertes`.
6. Tu peux aussi cocher **Ignorer la boîte de réception (Archiver)** pour ne pas polluer ta boîte principale —
   le script va quand même les trouver via le libellé.
7. Clique **Créer un filtre**.

Le script se connecte à ce libellé via IMAP (avec le `GMAIL_APP_PASSWORD` que tu as déjà), lit les emails non
lus, en extrait les liens d'offres, et les marque comme lus après traitement.

**c) Assure-toi qu'IMAP est activé** dans Gmail : Paramètres → Transfert et POP/IMAP → Activer IMAP.

## Étape 4 — Ajouter les 2 nouveaux secrets GitHub

Settings → Secrets and variables → Actions → New repository secret :

| Nom | Valeur |
|---|---|
| `ADZUNA_APP_ID` | ton Application ID Adzuna |
| `ADZUNA_APP_KEY` | ta Application Key Adzuna |

(Les secrets `FT_*` et `GMAIL_*` existent déjà — rien à changer pour eux.)

## Étape 5 — Pousser et tester

```bash
git add .
git commit -m "Ajout Adzuna + alertes email natives, élargissement postes/zones"
git push
```

Puis Actions → Veille emploi → Run workflow, et regarde les logs : tu dois voir une ligne par source
(`[FT]`, `[ADZUNA]`, `[ALERTES EMAIL]`) indiquant ce qui a été trouvé ou pourquoi une source a été ignorée.

## Comment fonctionne la rotation (pour comprendre les logs)

- **France Travail** : toutes les combinaisons (10 postes × 5 zones = 50) sont interrogées à *chaque* run
  de 30 min — pas de quota strict à ménager.
- **Adzuna** : une seule combinaison par run, mais *seulement* sur le run pivot de chaque heure (`xx:00`).
  Le script mémorise sa position dans `data/rotation_state.json` et reprend où il s'était arrêté. Cycle
  complet des 50 combinaisons en un peu plus de 2 jours.
- **Alertes email** : traité à chaque run (c'est gratuit, juste une lecture de boîte mail), mais ne
  remonte que ce que les plateformes elles-mêmes t'ont déjà envoyé par email.

## Prochaine étape

Génération de CV sur-mesure (Claude API + LaTeX) + dépôt en brouillon Gmail à valider avant envoi.

---

# Phase 3 — Génération de CV sur-mesure

Pour chaque nouvelle offre retenue **avec une vraie description** (France Travail, Adzuna —
les offres via alertes email n'ont qu'un lien, pas de texte exploitable), le pipeline :

1. Envoie à l'API Claude ton CV maître (`templates/cv_master_body.tex`) + la description de l'offre.
2. Récupère une version adaptée : puces réordonnées, section COMPÉTENCES réorganisée, phrase
   cible du PROFIL réécrite — **jamais d'ajout de compétence, outil ou expérience absente du CV
   maître**. C'est encodé en dur dans le prompt système (`CV_SYSTEM_PROMPT` dans `poll_jobs.py`).
3. Compile le résultat en PDF (le préambule LaTeX, `templates/cv_preamble.tex`, n'est **jamais**
   touché par Claude — seul le contenu peut changer, jamais la mise en forme).
4. T'envoie ce PDF par email, avec un sujet préfixé `[BROUILLON À VALIDER]` et un rappel explicite
   de le relire avant tout envoi à un recruteur.

## Pourquoi un email "brouillon" plutôt qu'un vrai brouillon dans Gmail

Un vrai brouillon dans le dossier Brouillons de Gmail nécessite l'API Gmail complète (authentification
OAuth2, écran de consentement Google Cloud) — plus long à mettre en place. Le compromis retenu
aujourd'hui : un email qui t'es envoyé à toi-même, avec le PDF en pièce jointe et un sujet qui ne
laisse aucune ambiguïté. Le résultat pratique est le même (rien ne part sans que tu relises et
transfères toi-même), juste avec 5 minutes de setup au lieu de 30. On pourra basculer vers de vrais
brouillons Gmail plus tard si tu veux ce confort en plus.

## Étape 1 — Créer une clé API Anthropic (5 min)

1. Va sur https://console.anthropic.com et crée un compte (ou connecte-toi).
2. **Settings → API Keys → Create Key**.
3. Copie la clé — c'est `ANTHROPIC_API_KEY`. Ajoute un peu de crédit prépayé (quelques euros
   suffisent largement : chaque génération de CV coûte de l'ordre du centime).

## Étape 2 — Ajouter le secret GitHub

Settings → Secrets and variables → Actions → New repository secret : `ANTHROPIC_API_KEY`.

C'est le seul secret à ajouter — le workflow détecte automatiquement sa présence pour activer
à la fois l'installation de LaTeX et la génération de CV.

## Étape 3 — Pousser et tester

```bash
git add .
git commit -m "Ajout génération de CV sur-mesure (Claude API + LaTeX)"
git push
```

Actions → Veille emploi → Run workflow. Le premier run avec LaTeX sera plus lent (~1-2 min de plus,
installation des paquets — mis en cache ensuite pour accélérer les runs suivants). Regarde les logs
pour la ligne `[CV] Brouillon envoyé pour : ...` — si tu ne vois que `[CV] ANTHROPIC_API_KEY absent`,
le secret n'a pas été détecté (vérifie l'orthographe exacte).

## Limite connue

Les offres remontées par les **alertes email** (LinkedIn, WTTJ, HelloWork, JobTeaser) n'ont pas de
description exploitable pour l'instant — seulement un lien. Le CV n'est donc généré que pour les
offres via France Travail et Adzuna. Une amélioration possible plus tard : aller chercher la
description en visitant le lien de l'offre, mais ça complique nettement les choses (chaque site a
sa propre structure de page, et certains bloquent les requêtes automatisées) — à évaluer si le
volume d'offres email le justifie.
