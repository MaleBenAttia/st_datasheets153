# Problèmes et Solutions — Extraction de tables STM32

## 1. Fausses continuations (table_42/43)

**Problème :** `_headers_differ` comparait les en-têtes index par index.
Quand une colonne était fusionnée (colspan) dans la page de continuation,
le décalage d'index faisait détecter une différence (ex: colonne "Conditions"
manquante → toutes les colonnes suivantes décalées → fausse non-correspondance
→ table rejetée).

**Solution :** Comparaison ensembliste (Jaccard) au lieu d'index par index.
On calcule `|A ∩ B| / |A ∪ B|` sur les ensembles de textes d'en-têtes dédupliqués.
Seuil : `jaccard < 0.50` → headers différents, sinon identiques.

**Fichier :** `continuation.py:_headers_differ`

---

## 2. Continuation rejetée malgré en-têtes identiques (table_58)

**Problème :** `_is_continuation_page` rejetait la continuation quand
`|col_count - expected| > 2`, même si les en-têtes étaient identiques.
Table_58 passait de 7 à 12 colonnes (colonnes fusionnées qui "s'ouvrent"
sur la page de continuation) → rejetée.

**Solution :** Quand les ensembles d'en-têtes (dédupliqués) sont strictement
identiques entre `raw_header` et `base_header`, on accepte la continuation
quel que soit l'écart de `col_count`.

**Fichier :** `continuation.py:_is_continuation_page`

---

## 3. Colonnes fusionnées mal expansées dans la continuation

**Problème :** `find_continuations` utilisait `_expand_cont_row` qui répétait
la dernière valeur connue pour remplir les cellules vides. Pour les colonnes
fusionnées horizontalement (ex: 6× "Conditions"), la cellule unique n'était
pas propagée aux 6 colonnes → données manquantes.

**Solution :** Nouvelle fonction `_expand_cont_row_by_x0s` qui utilise les
coordonnées x0 des colonnes de la page de base (`base_x0s`) pour décider
combien de fois répéter chaque valeur dans la page de continuation.
Remplace `_expand_cont_row` quand `base_x0s` et `cont_x0s` sont disponibles.

**Fichier :** `continuation.py:_expand_cont_row_by_x0s`

---

## 4. Lignes au-dessus de la légende (5 bugs F4+U0)

**Problème :** Quand 2 tables cohabitent sur la même page, pdfplumber les
fusionne en une seule `raw_table`. Le filtre caption supprime les lignes
de l'AUTRE table (au-dessus de la légende cible), mais ne trouve AUCUNE
ligne pour la table cible → `raw_table = []`. Puis `body_on_next_page`
tente la page suivante mais échoue aussi (autre table).

**Cas concrets :**
- F4 stm32f469ae / stm32f479ag : table_76 (Ethernet MAC MII) page 155
  avec table_75 (RMII) → corps de table_76 sur page 156 mais rejeté car
  table_77 aussi présente
- U0 stm32u073c8 / stm32u083cc : table_67 (NRST pin) page 86 avec
  continuation de table_66 → corps sur page 87
- U0 stm32u031c6 : table_65 (NRST pin) page 80 → corps sur page 81

**Solution 4a — Re-extraction sous caption_y (même page) :**
Quand toutes les lignes sont au-dessus de `caption_y`, on recadre la page
à partir de `caption_y - 5` et on ré-appelle `_extract_from_page` sur
cette zone recadrée (permet de capturer la table cible sans l'autre table).

**Solution 4b — Guard body_on_next_page relaxé :**
L'ancien guard rejetait la page suivante si **n'importe quel** autre numéro
de table était présent (`any(n != cur_num for n in other_nums)`).
Nouveau guard : on rejette seulement si la table cible **n'est pas** présente
(`other_nums and cur_num not in other_nums`). Ceci permet à table_76 d'être
extraite de la page 156 même si table_77 y apparaît aussi.

**Fichier :** `grid_extractor.py`

---

## 5. Headers avec codes commande au lieu de noms STM32

**Problème :** Certains datasheets (N6 stm32n645a0 table_2) utilisent des
codes commande `Q3H0X546N 23MTS` comme en-têtes de colonnes, sans le nom
du device STM32. Le code `23MTS` (date code) polluait l'en-tête.

**Solution :** Après la détection des en-têtes, on applique une regex
`Q3H0[A-Z0-9]+` pour détecter les codes commande. On extrait le nom STM32
depuis la légende via `STM32[A-Za-z0-9]+`. Les headers sont remplacés par
`"STM32N645xx (Q3H0X546N)"`.

**Fichier :** `grid_extractor.py`

---

## Résultat final

| Famille | OK | FAILED | Notes |
|---------|----|--------|-------|
| C0 | 5 | 0 | ✅ |
| C5 | 6 | 0 | ✅ |
| F0 | 13 | 0 | ✅ |
| F3 | 14 | 0 | ✅ |
| **F4** | **14** | **0** | ✅ 2 bugs (table_76) fixés |
| **U0** | **2** | **1** | ✅ 2 bugs (table_65, table_67) fixés — reste table_9 (1 ligne "X: supported", non extractable comme grille) |
| N6 | 1 | 0 | ✅ + headers nettoyés |
| **Total** | **55** | **1** | 5 bugs sur 6 corrigés |
