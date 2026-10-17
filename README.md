# NDVI mensuel par IRIS en Île-de-France

Calcule, pour chaque mois d'octobre 2022 à septembre 2026, le **NDVI moyen de chaque IRIS d'Île-de-France** (5 265 IRIS) à partir des images satellite **Sentinel-2** du programme Copernicus, puis produit une **carte HTML interactive** :

- choix de l'**année** et du **mois** affichés via deux listes déroulantes ;
- couleur de chaque IRIS selon son NDVI moyen (rouge = peu de végétation, vert = beaucoup) ;
- info-bulle au survol : commune, IRIS, code IRIS, NDVI moyen, nombre de pixels, % de nuages ;
- bouton **« Exporter le mois en CSV »** qui télécharge les données du mois affiché.

---

## 1. Installation

Python 3.10 ou plus. Depuis le dossier du projet :

```bash
pip install sentinelhub geopandas rasterio pandas numpy shapely
```

**Contours des IRIS** (non inclus dans le dépôt : 167 Mo, au-delà de la limite de GitHub). Télécharger **CONTOURS-IRIS®, édition 2026, format GeoPackage, Lambert-93, France métropolitaine** sur <https://geoservices.ign.fr/contoursiris>, puis décompresser l'archive dans `data/`. Le script cherche automatiquement le fichier `iris.gpkg` dans ce dossier et ses sous-dossiers.

**Déjà inclus dans le dépôt** : les résultats des 48 mois (`data/ndvi_monthly/`) et la carte (`ndvi_iris_idf.html`). Il suffit donc des contours IRIS pour régénérer la carte avec `python main.py --map-only`, sans identifiants Copernicus ni téléchargement. Les tuiles Sentinel-2 (~3 Go) ne sont pas versionnées ; `python main.py` les retélécharge si besoin.

## 2. Identifiants Copernicus

Le téléchargement passe par l'API **Sentinel Hub du Copernicus Data Space Ecosystem** (gratuite dans la limite d'un quota mensuel).

1. Créer un compte sur <https://dataspace.copernicus.eu>.
2. Aller sur <https://shapps.dataspace.copernicus.eu/dashboard> → **User settings → OAuth clients → + Create**.
3. Noter le **Client ID** (commence par `sh-`) et le **Client secret** (affiché une seule fois).

> ⚠️ Ce ne sont **pas** l'e-mail et le mot de passe du compte : ceux-ci sont refusés (`invalid_client`).

Le script demande ces deux valeurs au lancement. Pour ne pas les retaper, on peut les définir avant :

```bash
# Git Bash
export SH_CLIENT_ID="sh-xxxxxxxx-..."
export SH_CLIENT_SECRET="..."
```
```powershell
# PowerShell
$env:SH_CLIENT_ID = "sh-xxxxxxxx-..."
$env:SH_CLIENT_SECRET = "..."
```

## 3. Utilisation

```bash
python main.py              # télécharge les mois manquants, calcule les moyennes, génère la carte
python main.py --map-only   # régénère uniquement la carte à partir des résultats déjà calculés
```

Puis ouvrir **`ndvi_iris_idf.html`** dans un navigateur (double-clic, pas besoin de serveur).

**Reprise automatique.** Chaque tuile téléchargée et chaque mois calculé sont enregistrés sur le disque. Si le script s'arrête (quota atteint, coupure réseau, Ctrl+C), il suffit de le relancer : il reprend là où il s'était arrêté sans rien retélécharger. Les mois déjà calculés sont visibles sur la carte, les autres sont grisés dans la liste.

**Durée et quota.** Une exécution complète représente environ 2 200 requêtes et ~8 400 unités de traitement (voir § 7), soit de l'ordre d'une heure et un peu moins que le quota gratuit mensuel (10 000 unités, remis à zéro le 1er de chaque mois).

### Fichiers produits

| Fichier / dossier | Contenu |
|---|---|
| `ndvi_iris_idf.html` | La carte interactive (autonome, ~8 Mo). |
| `data/ndvi_monthly/ndvi_AAAA-MM.csv` | Résultats d'un mois pour tous les IRIS (§ 5.2). |
| `data/ndvi_tiles_monthly/AAAA-MM/ndvi_X_Y.tif` | Rasters NDVI téléchargés (GeoTIFF Lambert-93, 20 m), un par tuile et par mois. Ouvrables dans QGIS. |

---

## 4. Paramètres (en haut de `main.py`)

| Variable | Valeur | Signification |
|---|---|---|
| `IDF_DEPARTEMENTS` | `75, 77, 78, 91, 92, 93, 94, 95` | Départements retenus (filtre sur les 2 premiers caractères de `code_insee`). |
| `FIRST_MONTH`, `LAST_MONTH` | `2022-10`, `2026-09` | Période traitée (bornes incluses), au format `AAAA-MM`. |
| `RESOLUTION` | `20` | Taille d'un pixel en mètres. |
| `TILE_SIZE` | `20000` | Côté d'une tuile de téléchargement en mètres (20 km → 1 000 × 1 000 pixels à 20 m). |
| `MAX_THREADS` | `4` | Nombre de tuiles téléchargées en parallèle. |
| `NODATA` | `-32768` | Valeur des pixels exclus (nuages, ombres…) dans les GeoTIFF. |
| `NDVI_SCALE` | `10000` | Facteur d'échelle : le NDVI est stocké en entier `NDVI × 10 000` (deux fois moins coûteux en quota qu'un nombre décimal). |
| `MIN_COVERAGE` | `0.5` | **Affichage uniquement** : un IRIS dont moins de 50 % de la surface est visible (donc plus de 50 % de nuages) apparaît en gris sur la carte. N'affecte pas les CSV. |
| `NDVI_COLOR_RANGE` | `(0.0, 0.8)` | Bornes de l'échelle de couleurs, identiques pour tous les mois afin de pouvoir les comparer. Les valeurs hors bornes prennent la couleur de la borne. |

Modifier la période ou la résolution crée de nouveaux calculs ; pour repartir de zéro après un changement de `RESOLUTION` ou `TILE_SIZE`, supprimer `data/ndvi_tiles_monthly/` et `data/ndvi_monthly/`.

---

## 5. Signification et calcul des variables

### 5.1 Au niveau du pixel

**Image utilisée.** Pour chaque mois et chaque tuile, Sentinel Hub constitue l'image à partir des scènes Sentinel-2 **niveau L2A** (réflectance de surface, corrigée de l'atmosphère) du mois, en prenant en priorité **la scène la moins nuageuse** (`MosaickingOrder.LEAST_CC`, d'après la couverture nuageuse indiquée dans les métadonnées de chaque scène). Il y a donc **une seule date par pixel et par mois**, et non une moyenne de toutes les images du mois.

**NDVI d'un pixel** (indice de végétation par différence normalisée) :

```
NDVI = (B08 − B04) / (B08 + B04)
```

- `B04` : réflectance dans le **rouge** (665 nm), absorbée par la chlorophylle ;
- `B08` : réflectance dans le **proche infrarouge** (842 nm), fortement réfléchie par la végétation.

Le NDVI varie de −1 à 1 : eau et surfaces artificielles < ~0,2 ; végétation clairsemée ~0,2–0,5 ; végétation dense (forêts, cultures en pleine croissance) > ~0,6.

**Pixels exclus.** Un pixel est mis à `NODATA` (donc exclu des moyennes) si l'une des conditions suivantes est vraie :

| Condition | Source |
|---|---|
| Pas d'image disponible (`dataMask = 0`) | Sentinel Hub |
| Classe SCL 0 : pas de donnée | Classification de scène Sentinel-2 (bande SCL, produite par l'ESA) |
| Classe SCL 1 : pixel saturé ou défectueux | idem |
| Classe SCL 3 : ombre de nuage | idem |
| Classes SCL 8 et 9 : nuage (probabilité moyenne / forte) | idem |
| Classe SCL 10 : cirrus (nuage fin d'altitude) | idem |
| Classe SCL 11 : neige ou glace | idem |
| `B08 + B04 = 0` (division impossible) | — |

**Résolution.** `B04` et `B08` sont acquises à 10 m, `SCL` à 20 m. Les images sont demandées directement à 20 m en Lambert-93 (EPSG:2154) ; Sentinel Hub ré-échantillonne au **plus proche voisin** (méthode par défaut) : chaque pixel de 20 m prend la valeur d'un des pixels de 10 m qu'il recouvre. À l'échelle d'un IRIS (≥ 22 pixels pour le plus petit, ~830 pour l'IRIS médian), la moyenne obtenue est très proche de celle à 10 m.

### 5.2 Au niveau de l'IRIS (fichiers `data/ndvi_monthly/ndvi_AAAA-MM.csv`)

**Appartenance d'un pixel à un IRIS.** Un pixel appartient à un IRIS si **son centre** est situé dans le polygone de l'IRIS. Chaque pixel appartient au plus à un IRIS. Les IRIS à cheval sur plusieurs tuiles sont traités correctement (les sommes sont cumulées sur toutes les tuiles).

Notations, pour un IRIS donné et un mois donné :

- `N_total` = nombre de pixels dont le centre est dans l'IRIS (ne dépend pas du mois) ;
- `N_valides` = nombre de ces pixels **non exclus** ce mois-là (§ 5.1) ;
- `NDVI_p` = NDVI du pixel `p`.

| Colonne | Calcul | Signification |
|---|---|---|
| `code_iris` | — | Code IRIS INSEE à 9 caractères (code commune + n° d'IRIS). |
| `ndvi_mean` | `Σ NDVI_p / N_valides` (somme sur les pixels valides) | NDVI moyen de la partie visible de l'IRIS, chaque pixel ayant le même poids (moyenne pondérée par la surface). Vide si `N_valides = 0`. Arrondi à 4 décimales. |
| `n_pixels` | `N_valides` | Nombre de pixels de 20 m (400 m² chacun) ayant servi au calcul. |
| `coverage` | `N_valides / N_total` | Part de l'IRIS visible (non masquée), entre 0 et 1. Arrondi à 3 décimales. |

### 5.3 Dans le CSV exporté depuis la carte

Le bouton **« Exporter le mois en CSV »** télécharge `ndvi_iris_idf_AAAA-MM.csv` pour le mois affiché, avec une ligne par IRIS (les 5 265, y compris ceux en gris sur la carte).

Format Excel français : séparateur `;`, virgule décimale, encodage UTF-8.

| Colonne | Calcul | Signification |
|---|---|---|
| `commune` | — | Nom de la commune (ou de l'arrondissement pour Paris). |
| `iris` | — | Nom de l'IRIS. |
| `code_iris` | — | Code IRIS INSEE. |
| `ndvi_moyen` | `ndvi_mean` arrondi à 3 décimales | NDVI moyen de la partie visible de l'IRIS (§ 5.2). Vide si aucun pixel valide. |
| `nombre_pixels` | `n_pixels` | Nombre de pixels de 20 m utilisés. |
| `nuages_pct` | `100 − arrondi(100 × coverage)` | **Pourcentage de l'IRIS masqué** ce mois-là : nuages, ombres de nuages, neige, et pixels sans donnée (§ 5.1). 0 = IRIS entièrement visible, 100 = entièrement masqué. |

Le `ndvi_moyen` est fourni quel que soit `nuages_pct` : c'est à l'utilisateur de choisir son seuil (par ex. ne garder que `nuages_pct ≤ 30`). Plus `nuages_pct` est élevé, moins la moyenne est représentative de l'ensemble de l'IRIS.

### 5.4 Sur la carte

| Élément | Calcul |
|---|---|
| Couleur d'un IRIS | `ndvi_mean` placé sur l'échelle rouge → jaune → vert entre `NDVI_COLOR_RANGE` (0 et 0,8). |
| IRIS en gris | `ndvi_mean` vide, ou `coverage < MIN_COVERAGE` (plus de 50 % de nuages). |
| « IRIS exploitables » | Nombre d'IRIS non gris pour le mois affiché. |
| « % de nuages en moyenne » | Moyenne simple de `nuages_pct` sur tous les IRIS (chaque IRIS compte pour un, quelle que soit sa taille). |
| Mois grisé dans la liste | Mois pas encore calculé, ou sans aucun pixel valide. |

---

## 6. Fonctionnement du code (`main.py`)

| Fonction | Rôle |
|---|---|
| `load_iris_idf()` | Lit la couche `contours_iris` du GeoPackage, garde les 8 départements, projette en Lambert-93. |
| `tile_grid()` | Découpe l'emprise de la région en tuiles de 20 km alignées sur la grille des pixels ; garde les 46 qui touchent l'Île-de-France. |
| `tile_labels()` | Pour chaque tuile, « dessine » les IRIS dans une grille de pixels (numéro de l'IRIS contenant le centre de chaque pixel). Fait une seule fois, valable pour tous les mois. |
| `download_month()` | Télécharge les tuiles manquantes d'un mois (en parallèle) et les enregistre en GeoTIFF. |
| `month_stats()` | Calcule `ndvi_mean`, `n_pixels` et `coverage` par IRIS en croisant les tuiles NDVI et les grilles d'IRIS. |
| `update_monthly_stats()` | Boucle sur les mois non encore calculés ; s'arrête proprement en cas d'erreur de quota. |
| `make_map()` | Assemble contours simplifiés (tolérance 15 m) et résultats mensuels dans la page HTML (Leaflet, fonds de carte IGN). |

Le calcul du NDVI et le masquage des nuages sont faits **côté serveur** par Sentinel Hub (script `EVALSCRIPT` dans `main.py`) ; seules les moyennes par IRIS sont calculées localement.

## 7. Coût en quota

Sentinel Hub décompte des **unités de traitement (PU)** :

```
PU par requête = (largeur × hauteur / 512²) × (nombre de bandes en entrée / 3) × facteur de sortie
```

Ici : 1 000 × 1 000 pixels, 3 bandes (`B04`, `B08`, `SCL` ; `dataMask` est gratuite), sortie entière 16 bits (facteur 1) :

```
1 000 000 / 262 144 × 1 × 1 ≈ 3,8 PU par tuile
× 46 tuiles ≈ 175 PU par mois
× 48 mois  ≈ 8 400 PU
```

Quota gratuit (compte Copernicus General User) : 10 000 PU et 10 000 requêtes par mois. À 10 m de résolution le coût serait multiplié par 4 (~33 700 PU).

## 8. Limites à connaître

- **Nuages** : un satellite optique ne voit pas à travers les nuages. En hiver, de nombreux IRIS peuvent avoir un `nuages_pct` élevé. Pistes d'amélioration : combiner toutes les images du mois (plus coûteux en quota) ou interpoler entre les mois voisins.
- **Une seule date par mois** : la valeur d'un mois reflète l'état de la végétation à la date de la scène retenue, qui peut varier d'un pixel à l'autre si plusieurs scènes sont nécessaires pour couvrir la région.
- **Contours fixes** : les contours IRIS de l'édition 2026 sont utilisés pour toutes les années.
- **Pixels en bordure** : un pixel à cheval sur deux IRIS est attribué entièrement à celui qui contient son centre ; l'effet est négligeable sauf pour les très petits IRIS (voir `nombre_pixels`).

## 9. Sources

- Images : Copernicus Sentinel-2 L2A, via [Copernicus Data Space Ecosystem – Sentinel Hub](https://dataspace.copernicus.eu/ecosystem/services/sentinel-hub).
- Contours : IGN – CONTOURS-IRIS® (édition 2026, GeoPackage, Lambert-93).
- Fonds de carte : IGN Géoplateforme (Plan IGN, photographies aériennes).
- Définition des PU : [documentation Copernicus](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/ProcessingUnit.html).
