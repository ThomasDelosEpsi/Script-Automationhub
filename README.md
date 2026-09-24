# Script Automationhub

Ensemble de scripts Python pour extraire les données d'un AutomationHub (export CSV des automatisations, idéations) et les indexer dans une base de données PostgreSQL avec support des embeddings vectoriels (pgvector), en s'appuyant sur l'API UiPath et un modèle Mistral pour l'enrichissement des données. Inclut aussi un script de migration entre tenants.

## Stack

- Python (requests, pandas, SQLAlchemy, pgvector)
- PostgreSQL
- Docker Compose
- API UiPath Orchestrator / API Mistral

## Contenu

- `Main.py` — script principal d'extraction/indexation
- `migrate_tenant.py` — migration de données entre tenants
- `docker-compose.yml` — service(s) associés (ex. base de données)
- `*.csv` — exports de données AutomationHub

## Configuration

Les identifiants et URLs (token UiPath, clé API Mistral, chaîne de connexion base de données, proxy) sont lus depuis un fichier `.env` non versionné.
