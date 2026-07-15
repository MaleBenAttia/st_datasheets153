# STM32 Datasheet Table Extractor & RAG Transformer

[![GitHub Repo](https://img.shields.io/badge/GitHub-Repository-blue?logo=github)](https://github.com/MaleBenAttia/st_datasheets153)
**Depot public :** [MaleBenAttia/st_datasheets153](https://github.com/MaleBenAttia/st_datasheets153)

Pipeline complet d'extraction automatique de tableaux depuis les datasheets
PDF STMicroelectronics (STM32) et de transformation en chunks optimises pour
l'indexation vectorielle (RAG avec ChromaDB, Qdrant, Pinecone, etc.).

**Chiffres cles :** 185 datasheets, 20 familles STM32, extraction 100% high
confidence, 0 erreur, 0 valeur null.

---

## Table des matieres

1. [Pre-requis](#pre-requis)
2. [Installation](#installation)
3. [Arborescence du projet](#arborescence-du-projet)
4. [Le Pipeline en 3 etapes](#le-pipeline-en-3-etapes)
5. [Toutes les commandes](#toutes-les-commandes)
6. [Moteur d'extraction - Architecture technique](#moteur-dextraction--architecture-technique)
7. [Format de sortie JSON brut (outJason)](#format-de-sortie-json-brut-outjason)
8. [Format de sortie RAG (RagJason)](#format-de-sortie-rag-ragjason)
9. [Categories de filtrage RAG](#categories-de-filtrage-rag)
10. [Exemple d'utilisation ChromaDB](#exemple-dutilisation-chromadb)

---

## Pre-requis

- **Python** 3.10 ou superieur
- **OS** : Windows 10/11 (teste), Linux/macOS (compatible)
- **Espace disque** : ~500 Mo pour les PDFs + ~200 Mo pour les JSON generes

---

## Installation

```powershell
# 1. Creer un environnement virtuel Python
python -m venv venv

# 2. Activer l'environnement virtuel
.\venv\Scripts\activate

# 3. Installer les dependances (pdfplumber + pydantic)
pip install -r table_extractor_raw\requirements.txt
```

**Dependances :**
| Paquet       | Version min. | Role                                        |
|--------------|-------------|---------------------------------------------|
| `pdfplumber` | >= 0.11     | Extraction geometrique des tableaux PDF     |
| `pydantic`   | >= 2.0      | Validation structurelle des JSON de sortie  |

---

## Arborescence du projet

```
st_datasheets153/
|
|-- app.py                      # Point d'entree CLI (wrapper principal)
|-- check_quality.py            # Audit qualite des JSON extraits
|-- aggregate_stats.py          # Consolidation des stats d'extraction
|-- rag_transformer.py          # Transformation JSON -> chunks RAG
|-- .gitignore                  # Fichiers/dossiers exclus du versioning
|-- README.md                   # Ce fichier
|
|-- table_extractor_raw/        # === MOTEUR D'EXTRACTION (coeur) ===
|   |-- main.py                 #   Orchestrateur CLI interne
|   |-- config.py               #   Parametres globaux et seuils
|   |-- requirements.txt        #   Dependances Python
|   |-- core/                   #   Modules du moteur
|   |   |-- __init__.py
|   |   |-- toc_detector.py     #     Detection des tables via TOC/scan
|   |   |-- grid_extractor.py   #     Extraction geometrique spatiale
|   |   |-- continuation.py     #     Fusion multi-pages
|   |   |-- quality_flags.py    #     Evaluation de la confiance
|   |   |-- glyph_fixer.py      #     Correction des glyphes Unicode
|   |   |-- schema.py           #     Modele Pydantic de validation
|
|-- DataSHEET/                  # === PDFs SOURCES (non versionnes) ===
|   |-- C0/                     #   Famille C0 (stm32c011d6.pdf, ...)
|   |-- F1/                     #   Famille F1 (stm32f103rc.pdf, ...)
|   |-- G0/                     #   Famille G0 (stm32g081cb.pdf, ...)
|   |-- ...                     #   20 familles au total
|
|-- outJason/                   # === SORTIE BRUTE (generee) ===
|   |-- <family>/<pdf_name>/
|   |   |-- table_1.json        #     JSON individuel par table
|   |   |-- table_2.json
|   |   |-- ...
|   |   |-- _all_tables.json    #     Toutes les tables du PDF
|   |   |-- _run_report.json    #     Rapport d'execution du PDF
|
|-- RagJason/                   # === CHUNKS RAG (generes) ===
|   |-- stm32c011d6.json        #     Un fichier par datasheet
|   |-- stm32f103rc.json
|   |-- stm32g081cb.json
|
|-- global_extraction_stats.json  # Rapport de sante global (genere)
```

---

## Le Pipeline en 3 etapes

### Etape 1 : Extraction brute (app.py)

Parcourt les PDF, detecte les tableaux via le sommaire (TOC) ou par scan
inline, puis extrait chaque tableau avec un moteur geometrique spatial.

**Entree :** Fichiers PDF dans `DataSHEET/`
**Sortie :** Fichiers JSON dans `outJason/`

### Etape 2 : Consolidation des statistiques (aggregate_stats.py)

Regroupe les metadonnees d'extraction (methode, confiance, taux de vide,
warnings) dans un fichier unique. Ce fichier est destine au monitoring et
au debug, PAS a l'indexation vectorielle.

**Entree :** `outJason/` (fichiers `_run_report.json` et `_all_tables.json`)
**Sortie :** `global_extraction_stats.json`

### Etape 3 : Transformation RAG (rag_transformer.py)

Convertit les tableaux bruts en chunks RAG optimises pour la recherche
semantique. Nettoie les metadonnees internes, categorise les tables,
extrait les mots-cles, et genere un fichier JSON par datasheet.

**Entree :** `outJason/` (fichiers `_all_tables.json`)
**Sortie :** `RagJason/` (un fichier `.json` par datasheet)

---

## Toutes les commandes

### Extraction (app.py)

```powershell
# --- Extraire UN SEUL PDF (ideal pour tester) ---
.\venv\Scripts\python.exe app.py --pdf DataSHEET/C0/stm32c011d6.pdf
.\venv\Scripts\python.exe app.py --pdf DataSHEET/F1/stm32f103rc.pdf
.\venv\Scripts\python.exe app.py --pdf DataSHEET/G0/stm32g081cb.pdf

# --- Extraire TOUTE UNE FAMILLE (ex: les 20 PDFs de la famille F1) ---
.\venv\Scripts\python.exe app.py --family C0
.\venv\Scripts\python.exe app.py --family F1
.\venv\Scripts\python.exe app.py --family G0
.\venv\Scripts\python.exe app.py --family H7
.\venv\Scripts\python.exe app.py --family L4
.\venv\Scripts\python.exe app.py --family U5

# --- Extraire TOUS les 185 PDFs d'un coup ---
.\venv\Scripts\python.exe app.py --all
```

**Familles disponibles (20) :**
`C0`, `C5`, `F0`, `F1`, `F2`, `F3`, `F4`, `F7`, `G0`, `G4`,
`H5`, `H7`, `L0`, `L1`, `L4`, `L5`, `N6`, `U0`, `U3`, `U5`

### Audit qualite (check_quality.py)

```powershell
# --- Verifier la qualite de TOUS les JSON extraits ---
.\venv\Scripts\python.exe check_quality.py

# --- Verifier un dossier specifique ---
.\venv\Scripts\python.exe check_quality.py outJason\F1\stm32f103rc
```

**Ce que le rapport affiche :**
- Nombre total de tables extraites
- Tables en continuation (multi-pages)
- Tables a confiance medium ou low
- Tables vides (0 lignes)
- Taux de cellules vides anormalement eleve (> 30%)

### Consolidation des stats (aggregate_stats.py)

```powershell
# --- Generer le rapport global de sante ---
.\venv\Scripts\python.exe aggregate_stats.py
```

**Sortie :** `global_extraction_stats.json` contenant pour chaque PDF :
- Nombre de tables trouvees / extraites
- Compteur high / medium / low / failed
- Pour chaque table : methode, confiance, taux de vide, warnings

### Transformation RAG (rag_transformer.py)

```powershell
# --- Generer les chunks RAG (un fichier par datasheet) ---
.\venv\Scripts\python.exe rag_transformer.py
```

**Sortie :** Dossier `RagJason/` avec un fichier JSON par datasheet.

### Commande "Tout-en-un"

```powershell
# --- Pipeline COMPLET : Extraction + Stats + RAG ---
.\venv\Scripts\python.exe app.py --all ; .\venv\Scripts\python.exe aggregate_stats.py ; .\venv\Scripts\python.exe rag_transformer.py
```

### Test rapide (3 datasheets)

```powershell
# --- Test sur 3 familles differentes ---
.\venv\Scripts\python.exe app.py --pdf DataSHEET/C0/stm32c011d6.pdf
.\venv\Scripts\python.exe app.py --pdf DataSHEET/F1/stm32f103rc.pdf
.\venv\Scripts\python.exe app.py --pdf DataSHEET/G0/stm32g081cb.pdf
.\venv\Scripts\python.exe check_quality.py
.\venv\Scripts\python.exe aggregate_stats.py
.\venv\Scripts\python.exe rag_transformer.py
```

### Nettoyage (repartir de zero)

```powershell
# --- Supprimer toutes les donnees generees ---
Remove-Item -Recurse -Force outJason, RagJason -ErrorAction SilentlyContinue
Remove-Item -Force global_extraction_stats.json -ErrorAction SilentlyContinue
```

---

## Moteur d'extraction - Architecture technique

### Les 6 piliers geometriques

L'extracteur ne "devine" pas les mots : il agit comme un scanner optique
base sur les lignes tracees dans le PDF.

| # | Pilier                           | Description                                                     |
|---|----------------------------------|-----------------------------------------------------------------|
| 1 | **Textes rotatifs**              | Mapping des mots verticaux (90 degres) dans la bonne colonne    |
| 2 | **En-tetes structurels**         | Detection dynamique de la profondeur (1 a 3 lignes)             |
| 3 | **Grille X calculee**            | Centres de colonnes calcules mathematiquement                   |
| 4 | **Continuation multi-pages**     | Fusion des tableaux etales sur 2+ pages                         |
| 5 | **Propagation horizontale**      | Remplissage des cellules fusionnees (colspan)                   |
| 6 | **Propagation verticale**        | Remplissage des cellules fusionnees (rowspan)                   |

### Extraction Spatiale des En-tetes (avance)

Quand les lignes graphiques manquent dans l'en-tete du PDF (cas frequent
dans les tables "device features"), le moteur recupere les mots un par un
avec leurs coordonnees (x, y) et les projette geometriquement vers le
centre de la colonne de donnees la plus proche.

### Detection multi-lignes du sommaire (avance)

Le moteur gere les entrees de TOC dont le titre est trop long et se
retrouve coupe sur deux lignes dans le PDF, grace a une machine a etats
avec buffer d'accumulation.

### Correction des textes inverses

Certains textes pivotes dans le PDF sont encodes a l'envers par le
moteur PDF (ex: "sremiT" au lieu de "Timers"). Le pipeline :
- **Conserve** le texte original intact dans `raw_json` (fidelite)
- **Ajoute** la version corrigee dans le champ `document` du RAG (recherche)

---

## Format de sortie JSON brut (outJason)

Chaque table extraite produit un fichier JSON individuel :

```json
{
  "table_id": "table_11",
  "caption": "Table 11. Alternate function mapping",
  "pdf_name": "stm32g081cb",
  "family": "G0",
  "page": 45,
  "merged_pages": [45, 46],
  "headers": ["Port", "Pin", "AF0", "AF1", "AF2"],
  "rows": [
    ["GPIOA", "PA0", "SPI2_SCK", "USART2_CTS", "TIM2_CH1"],
    ["GPIOA", "PA1", "SPI1_SCK", "USART2_DE", "TIM2_CH2"]
  ],
  "extraction_method": "pdfplumber",
  "extraction_confidence": "high",
  "empty_cell_ratio": 0.0,
  "col_count": 5,
  "warnings": [],
  "datasheet_metaData": {
    "pdf_name": "stm32g081cb",
    "table_id": "table_11",
    "is_continued": true,
    "pages": [45, 46],
    "rows_count": 65,
    "cols_count": 5,
    "confidence": "high",
    "empty_cell_ratio": 0.0
  }
}
```

**Champs importants :**
| Champ                    | Type     | Description                                      |
|--------------------------|----------|--------------------------------------------------|
| `table_id`               | string   | Identifiant unique (table_1, table_2, ...)       |
| `caption`                | string   | Legende complete de la table                     |
| `pdf_name`               | string   | Nom du PDF source (sans extension)               |
| `family`                 | string   | Famille STM32 (C0, F1, G0, H7, ...)             |
| `page`                   | int      | Page de debut (1-indexe)                         |
| `merged_pages`           | int[]    | Pages sur lesquelles la table s'etale            |
| `headers`                | string[] | En-tetes des colonnes                            |
| `rows`                   | string[][] | Donnees ligne par ligne                        |
| `extraction_method`      | string   | "pdfplumber" ou "pdfplumber_text"                |
| `extraction_confidence`  | string   | "high", "medium", ou "low"                       |
| `empty_cell_ratio`       | float    | Ratio de cellules vides (0.0 = parfait)          |

---

## Format de sortie RAG (RagJason)

Chaque fichier dans `RagJason/` contient un array d'objets chunk :

```json
{
  "id": "stm32g081cb_table_12_part1",
  "document": "Table 12. Alternate function mapping (page 45). PA0, PA1, PB5, SPI1_MISO, USART2_CTS, TIM2_CH1...",
  "metadata": {
    "pdf_name": "stm32g081cb",
    "table_id": "table_12",
    "family": "G0",
    "page": 45,
    "category": "pinout_af_mapping",
    "row_count": 20,
    "col_count": 8,
    "pins": "PA0,PA1,PB5",
    "raw_json": "{...}"
  }
}
```

**Champs du chunk :**
| Champ               | Type   | Role                                                    |
|---------------------|--------|---------------------------------------------------------|
| `id`                | string | Identifiant unique du chunk                             |
| `document`          | string | Texte dense pour l'embedding vectoriel                  |
| `metadata.pdf_name` | string | Filtre : nom du composant                               |
| `metadata.family`   | string | Filtre : famille STM32                                  |
| `metadata.category` | string | Filtre : type de table (voir categories ci-dessous)     |
| `metadata.page`     | int    | Filtre : page du PDF                                    |
| `metadata.pins`     | string | Filtre : liste CSV des pins (PA0,PB5,...)               |
| `metadata.row_count`| int    | Info : nombre de lignes dans ce chunk                   |
| `metadata.col_count`| int    | Info : nombre de colonnes                               |
| `metadata.raw_json` | string | JSON brut pur (sans metadonnees d'extraction)           |

---

## Categories de filtrage RAG

Le champ `metadata.category` est determine automatiquement par regex
sur le `caption` de la table :

| Categorie             | Declencheur (regex sur caption)                | Exemple de table                              |
|-----------------------|------------------------------------------------|-----------------------------------------------|
| `pinout_af_mapping`   | "alternate function"                           | Table 12. Alternate function mapping          |
| `pinout_description`  | "assignment and description"                   | Table 8. Pin assignment and description       |
| `electrical_spec`     | "characteristics", "consumption", "conditions" | Table 30. I2C characteristics                 |
| `mechanical_package`  | "mechanical data", "package"                   | Table 65. LQFP48 mechanical data              |
| `device_features`     | "device features", "peripheral counts"         | Table 2. Device features and peripheral counts|
| `changelog`           | "revision history"                             | Table 71. Document revision history           |
| `general`             | tout le reste                                  | Table 5. Timer feature comparison             |

---

## Exemple d'utilisation ChromaDB

### Ingestion des chunks

```python
import json
import chromadb

client = chromadb.PersistentClient(path="./chroma_db")
collection = client.get_or_create_collection("stm32_tables")

# Charger un fichier RAG
with open("RagJason/stm32g081cb.json", "r", encoding="utf-8") as f:
    chunks = json.load(f)

# Indexer tous les chunks
collection.add(
    ids=[c["id"] for c in chunks],
    documents=[c["document"] for c in chunks],
    metadatas=[{k: v for k, v in c["metadata"].items() if k != "raw_json"} for c in chunks],
)
```

### Requete selective (pre-filtrage + similarite)

```python
# Chercher les specs electriques du SPI sur la famille G0
results = collection.query(
    query_texts=["SPI clock frequency maximum"],
    n_results=3,
    where={
        "$and": [
            {"family": {"$eq": "G0"}},
            {"category": {"$eq": "electrical_spec"}}
        ]
    }
)
```

### Requete par pin specifique

```python
# Trouver toutes les alternate functions du pin PA0
results = collection.query(
    query_texts=["PA0 alternate function"],
    n_results=5,
    where={"category": {"$eq": "pinout_af_mapping"}}
)
```

### Requete par composant exact

```python
# Tout savoir sur le STM32F103RC
results = collection.query(
    query_texts=["power consumption standby mode"],
    n_results=3,
    where={"pdf_name": {"$eq": "stm32f103rc"}}
)
```
