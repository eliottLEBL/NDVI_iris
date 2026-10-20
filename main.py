"""Monthly mean Sentinel-2 NDVI per IRIS in Île-de-France, as an interactive HTML map.

Pipeline:
  1. Load the IGN CONTOURS-IRIS GeoPackage and keep the 8 Île-de-France départements.
  2. Cut the region into 20 km x 20 km tiles (Lambert-93). For every month of the period,
     download one 20 m NDVI raster per tile from the Copernicus Data Space Sentinel Hub
     Process API (least-cloudy Sentinel-2 L2A scene of the month, clouds masked with SCL).
     Tiles are cached on disk, so the script resumes where it stopped (quota, Ctrl+C...).
  3. Rasterize the IRIS once per tile, then for each month accumulate NDVI sum / valid pixel
     count per IRIS, giving a pixel-weighted mean and the share of the IRIS not under clouds.
  4. Write one CSV per month, and one per October-September year (mean of the months where the
     IRIS is mostly cloud-free).
  5. Build an HTML page (Leaflet) with year / period selectors (annual mean or one month) and a
     CSV export of the displayed period.

Usage:
  python main.py              download missing months, then rebuild the map
  python main.py --map-only   rebuild the map from the monthly CSVs, no download

Credentials: an OAuth client from https://shapps.dataspace.copernicus.eu/dashboard
(User settings > OAuth clients), from SH_CLIENT_ID / SH_CLIENT_SECRET or asked at startup.
"""

import json
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from getpass import getpass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from rasterio.features import rasterize
from rasterio.transform import from_origin
from sentinelhub import (
    CRS,
    BBox,
    DataCollection,
    MimeType,
    MosaickingOrder,
    SentinelHubDownloadClient,
    SentinelHubRequest,
    SHConfig,
)
from sentinelhub.exceptions import DownloadFailedException, SHRateLimitWarning
from shapely.geometry import box

# Copernicus caps requests per minute; sentinelhub then waits and retries by itself, so this is not an error
warnings.filterwarnings("ignore", category=SHRateLimitWarning)

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TILES_DIR = DATA_DIR / "ndvi_tiles_monthly"
MONTHLY_DIR = DATA_DIR / "ndvi_monthly"
ANNUAL_DIR = DATA_DIR / "ndvi_annual"
IRIS_GPKG = next(DATA_DIR.rglob("iris.gpkg"))
OUT_HTML = ROOT / "ndvi_iris_idf.html"

IDF_DEPARTEMENTS = ("75", "77", "78", "91", "92", "93", "94", "95")
FIRST_MONTH = "2022-10"
LAST_MONTH = "2026-09"
RESOLUTION = 20  # m: ~8 400 PU for 4 years, at least ~22 px for the smallest IRIS
TILE_SIZE = 20_000  # m -> 1000 x 1000 px per request
MAX_THREADS = 2  # the per-minute rate limit is the bottleneck, more threads only cause more retries

NODATA = -32768
NDVI_SCALE = 10_000  # NDVI stored as INT16 * 10000: half the processing units of FLOAT32
MIN_COVERAGE = 0.5  # below this share of cloud-free pixels, an IRIS is "cloudy" that month
YEAR_START_MONTH = 10  # years run from October to September: 4 full years over the period
MIN_MONTHS = 6  # display only: with fewer usable months in a year, an IRIS is shown as "no data"
NDVI_COLOR_RANGE = (0.0, 0.8)  # fixed so that months can be compared

EVALSCRIPT = f"""
//VERSION=3
function setup() {{
  return {{
    input: ["B04", "B08", "SCL", "dataMask"],
    output: {{ bands: 1, sampleType: "INT16" }}
  }};
}}
// SCL classes to drop: 0 no data, 1 saturated, 3 cloud shadow, 8-9 clouds, 10 cirrus, 11 snow
const INVALID = [0, 1, 3, 8, 9, 10, 11];
function evaluatePixel(s) {{
  if (!s.dataMask || INVALID.includes(s.SCL) || s.B08 + s.B04 === 0) return [{NODATA}];
  return [{NDVI_SCALE} * (s.B08 - s.B04) / (s.B08 + s.B04)];
}}
"""


def sentinelhub_config():
    # Environment variables if set, otherwise ask in the terminal
    client_id = os.environ.get("SH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SH_CLIENT_SECRET", "").strip()
    if not client_id:
        client_id = input("Copernicus OAuth client ID (sh-...): ").strip()
    if not client_secret:
        # getpass hides the input but hangs in Git Bash (mintty), where stdin is not a console
        ask = getpass if sys.stdin.isatty() else input
        client_secret = ask("Copernicus OAuth client secret: ").strip()
    if not client_id or not client_secret:
        raise SystemExit("A Copernicus Data Space OAuth client ID and secret are required.")
    config = SHConfig()
    config.sh_client_id = client_id
    config.sh_client_secret = client_secret
    config.sh_base_url = "https://sh.dataspace.copernicus.eu"
    config.sh_token_url = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    config.max_retries = 30  # give up after ~15 min of rate limiting (monthly quota exhausted)
    return config


def load_iris_idf():
    iris = gpd.read_file(IRIS_GPKG, layer="contours_iris")
    iris = iris[iris["code_insee"].str[:2].isin(IDF_DEPARTEMENTS)]
    return iris.to_crs(2154).sort_values("code_iris").reset_index(drop=True)


def all_months():
    return [str(p) for p in pd.period_range(FIRST_MONTH, LAST_MONTH, freq="M")]


def year_of(month):
    """Label 'AAAA-AAAA' of the October-September year containing the month 'AAAA-MM'."""
    start = (pd.Period(month, freq="M") - (YEAR_START_MONTH - 1)).year
    return f"{start}-{start + 1}"


def tile_grid(region):
    """20 km tiles aligned on the pixel grid, keeping only those touching the region."""
    xmin, ymin, xmax, ymax = region.bounds
    x0 = np.floor(xmin / TILE_SIZE) * TILE_SIZE
    y0 = np.floor(ymin / TILE_SIZE) * TILE_SIZE
    tiles = [
        box(x, y, x + TILE_SIZE, y + TILE_SIZE)
        for x in np.arange(x0, xmax, TILE_SIZE)
        for y in np.arange(y0, ymax, TILE_SIZE)
    ]
    return [t for t in tiles if t.intersects(region)]


def tile_transform(tile):
    xmin, _, _, ymax = tile.bounds
    return from_origin(xmin, ymax, RESOLUTION, RESOLUTION)


def tile_labels(iris, tiles):
    """For each tile, a raster holding the index (+1) of the IRIS containing each pixel centre."""
    shape = (TILE_SIZE // RESOLUTION, TILE_SIZE // RESOLUTION)
    labels = []
    for tile in tiles:
        idx = iris.sindex.query(tile)
        labels.append(rasterize(
            ((iris.geometry.iloc[i], int(i) + 1) for i in idx),
            out_shape=shape, transform=tile_transform(tile), fill=0, dtype="int16",
        ))
    return labels


def download_month(month, tiles, config, collection):
    """Download the month's missing NDVI tiles. Returns the tile paths."""
    folder = TILES_DIR / month
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / f"ndvi_{int(t.bounds[0])}_{int(t.bounds[1])}.tif" for t in tiles]
    missing = [(t, p) for t, p in zip(tiles, paths) if not p.exists()]
    if not missing:
        return paths

    period = pd.Period(month, freq="M")
    interval = (period.start_time.strftime("%Y-%m-%d"), period.end_time.strftime("%Y-%m-%d"))
    requests = [
        SentinelHubRequest(
            evalscript=EVALSCRIPT,
            input_data=[
                SentinelHubRequest.input_data(
                    data_collection=collection,
                    time_interval=interval,
                    mosaicking_order=MosaickingOrder.LEAST_CC,  # no maxcc: SCL masks the clouds
                )
            ],
            responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
            bbox=BBox(tile.bounds, crs=CRS(2154)),
            resolution=(RESOLUTION, RESOLUTION),
            config=config,
        ).download_list[0]
        for tile, _ in missing
    ]
    client = SentinelHubDownloadClient(config=config)

    def fetch(tile, path, request):
        # Each tile is saved as soon as it arrives, so an interruption loses at most the tiles in flight.
        # Written under a temporary name first: a half-written file must not look like a finished tile.
        ndvi = client.download([request])[0].astype(np.int16)
        tmp = path.with_suffix(".part")
        with rasterio.open(
            tmp, "w", driver="GTiff", height=ndvi.shape[0], width=ndvi.shape[1], count=1,
            dtype="int16", crs="EPSG:2154", nodata=NODATA, compress="deflate",
            transform=tile_transform(tile),
        ) as dst:
            dst.write(ndvi, 1)
        tmp.replace(path)

    executor = ThreadPoolExecutor(max_workers=MAX_THREADS)
    futures = [executor.submit(fetch, t, p, r) for (t, p), r in zip(missing, requests)]
    done = len(tiles) - len(missing)
    try:
        for future in as_completed(futures):
            future.result()  # re-raises download errors
            done += 1
            print(f"\r  {month} : {done}/{len(tiles)} tuiles", end="", flush=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        print("\r" + " " * 40 + "\r", end="", flush=True)
    return paths


def month_stats(iris, paths, labels, n_total):
    """Pixel-weighted mean NDVI, valid pixel count and cloud-free share per IRIS."""
    sums = np.zeros(len(iris))
    counts = np.zeros(len(iris), dtype=np.int64)
    for path, lab in zip(paths, labels):
        with rasterio.open(path) as src:
            ndvi = src.read(1)
        valid = (lab > 0) & (ndvi != NODATA)
        sums += np.bincount(lab[valid] - 1, weights=ndvi[valid], minlength=len(iris))
        counts += np.bincount(lab[valid] - 1, minlength=len(iris))

    with np.errstate(invalid="ignore", divide="ignore"):
        return pd.DataFrame({
            "code_iris": iris["code_iris"],
            "ndvi_mean": np.where(counts > 0, sums / counts / NDVI_SCALE, np.nan).round(4),
            "n_pixels": counts,
            "coverage": np.where(n_total > 0, counts / n_total, 0).round(3),
        })


def update_monthly_stats(iris):
    """Download and compute every month that has no CSV yet. Stops cleanly on quota errors."""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    todo = [m for m in all_months() if not (MONTHLY_DIR / f"ndvi_{m}.csv").exists()]
    if not todo:
        print("Tous les mois sont déjà calculés.")
        return

    config = sentinelhub_config()
    collection = DataCollection.SENTINEL2_L2A.define_from("s2l2a_cdse", service_url=config.sh_base_url)
    tiles = tile_grid(iris.union_all())
    labels = tile_labels(iris, tiles)
    n_total = sum(np.bincount(lab[lab > 0] - 1, minlength=len(iris)) for lab in labels)
    print(f"{len(todo)} mois à traiter, {len(tiles)} tuiles de {TILE_SIZE // 1000} km par mois")

    for i, month in enumerate(todo, 1):
        try:
            paths = download_month(month, tiles, config, collection)
        except DownloadFailedException as error:
            print(f"\nArrêt sur {month} : {error}")
            print("Quota mensuel probablement atteint. Relancez le script plus tard (quota remis à zéro"
                  " le 1er du mois) : il reprendra là où il s'est arrêté.")
            return
        except KeyboardInterrupt:
            print(f"\nInterrompu sur {month}. Les tuiles déjà reçues sont gardées : relancez pour reprendre.")
            return
        stats = month_stats(iris, paths, labels, n_total)
        stats.to_csv(MONTHLY_DIR / f"ndvi_{month}.csv", index=False)
        usable = (stats["coverage"] >= MIN_COVERAGE).sum()
        print(f"[{i}/{len(todo)}] {month} : {usable}/{len(iris)} IRIS exploitables "
              f"({1 - stats['coverage'].mean():.0%} de nuages en moyenne)")


def load_monthly_stats(iris):
    """{month: DataFrame aligned on iris} for every month already computed."""
    results = {}
    for month in all_months():
        path = MONTHLY_DIR / f"ndvi_{month}.csv"
        if path.exists():
            df = pd.read_csv(path, dtype={"code_iris": str}).set_index("code_iris")
            results[month] = df.reindex(iris["code_iris"])
    return results


def annual_stats(monthly):
    """{year: DataFrame aligned on iris} for every year whose 12 months are computed.

    ndvi_mean is the mean of the monthly means over the months where the IRIS is usable
    (coverage >= MIN_COVERAGE), each month having the same weight; n_months counts those months.
    """
    by_year = {}
    for month, df in monthly.items():
        by_year.setdefault(year_of(month), []).append(df)
    results = {}
    for year, dfs in by_year.items():
        if len(dfs) < 12:
            continue
        ndvi = pd.concat([df["ndvi_mean"].where(df["coverage"] >= MIN_COVERAGE) for df in dfs], axis=1)
        results[year] = pd.DataFrame({
            "ndvi_mean": ndvi.mean(axis=1).round(4),
            "n_months": ndvi.notna().sum(axis=1),
        })
    return results


def save_annual_stats(annual):
    ANNUAL_DIR.mkdir(parents=True, exist_ok=True)
    for year, df in annual.items():
        df.to_csv(ANNUAL_DIR / f"ndvi_{year}.csv")


def make_map(iris, monthly, annual, out_html):
    geo = iris[["code_iris", "nom_iris", "nom_commune", "geometry"]].copy()
    geo["geometry"] = geo.geometry.simplify(15).to_crs(4326)
    geo["geometry"] = shapely.transform(geo.geometry, lambda c: c.round(5))  # lighter HTML
    features = [
        {
            "type": "Feature",
            "properties": {"i": i, "c": row.nom_commune, "n": row.nom_iris, "k": row.code_iris},
            "geometry": shapely.geometry.mapping(row.geometry),
        }
        for i, row in enumerate(geo.itertuples())
    ]

    def ints(values):
        return [None if pd.isna(v) else int(v) for v in values]

    data = {
        month: {
            "ndvi": ints((df["ndvi_mean"] * 1000).round()),  # NDVI x 1000
            "px": ints(df["n_pixels"].fillna(0)),
            "cov": ints((df["coverage"].fillna(0) * 100).round()),  # %
        }
        for month, df in monthly.items()
        if df["n_pixels"].sum() > 0
    }
    annual_data = {
        year: {
            "ndvi": ints((df["ndvi_mean"] * 1000).round()),
            "nm": ints(df["n_months"]),  # usable months
        }
        for year, df in annual.items()
    }
    meta = {
        "vmin": NDVI_COLOR_RANGE[0],
        "vmax": NDVI_COLOR_RANGE[1],
        "minCov": round(MIN_COVERAGE * 100),
        "minMonths": MIN_MONTHS,
        "yearStart": YEAR_START_MONTH,
        "resolution": RESOLUTION,
    }

    def js(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    html = (
        HTML_TEMPLATE
        .replace("__GEO__", js({"type": "FeatureCollection", "features": features}))
        .replace("__DATA__", js(data))
        .replace("__ANNUAL__", js(annual_data))
        .replace("__META__", js(meta))
    )
    out_html.write_text(html, encoding="utf-8")
    print(f"Carte : {out_html} ({len(data)} mois et {len(annual_data)} années disponibles)")


HTML_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NDVI par IRIS</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .panel { background: #fff; color: #222; padding: 10px 12px; border-radius: 6px;
           box-shadow: 0 1px 5px rgba(0,0,0,.35); font-size: 13px; line-height: 1.5; }
  .panel h1 { font-size: 15px; margin: 0 0 8px; }
  .panel label { margin-right: 4px; }
  .panel select, .panel button { font: inherit; padding: 2px 4px; }
  .panel button { margin-top: 8px; cursor: pointer; }
  .panel .info { color: #555; margin-top: 6px; max-width: 260px; }
  .legend .bar { width: 220px; height: 12px; border: 1px solid #999; }
  .legend .ticks { display: flex; justify-content: space-between; width: 222px; }
  .legend .nodata { margin-top: 6px; display: flex; align-items: center; gap: 6px; }
  .legend .swatch { width: 14px; height: 12px; background: #bdbdbd; border: 1px solid #999; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const GEO = __GEO__;
const DATA = __DATA__;      // by month "AAAA-MM"
const ANNUAL = __ANNUAL__;  // by October-September year "AAAA-AAAA"
const META = __META__;

const MONTH_NAMES = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
                     "août", "septembre", "octobre", "novembre", "décembre"];
const STOPS = ["#a50026", "#f46d43", "#fee08b", "#d9ef8b", "#66bd63", "#006837"];  // RdYlGn
const RGB = STOPS.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
const NO_DATA_COLOR = "#bdbdbd";
const months = Object.keys(DATA).sort();

function ndviColor(v) {
  let t = (v - META.vmin) / (META.vmax - META.vmin);
  t = Math.max(0, Math.min(1, t)) * (RGB.length - 1);
  const i = Math.min(Math.floor(t), RGB.length - 2), f = t - i;
  return "rgb(" + RGB[i].map((a, k) => Math.round(a + (RGB[i + 1][k] - a) * f)).join(",") + ")";
}

// ---- Map and basemaps (IGN Géoplateforme: free, no key, works from a local file) ----
const map = L.map("map", { preferCanvas: true }).setView([48.75, 2.45], 9);
function ign(layer, format) {
  return L.tileLayer(
    "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=" + layer +
    "&STYLE=normal&TILEMATRIXSET=PM&FORMAT=" + format + "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
    { maxZoom: 19, attribution: '&copy; <a href="https://geoservices.ign.fr/">IGN</a> · Copernicus Sentinel-2' });
}
const plan = ign("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png").addTo(map);
const ortho = ign("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg");

// The displayed period is a month key "AAAA-MM" or a year key "AAAA-AAAA"
const isYear = key => key in ANNUAL;
const record = key => ANNUAL[key] || DATA[key];
const yearKeys = Object.keys(ANNUAL).sort();
let current = yearKeys.length ? yearKeys[yearKeys.length - 1] : months[months.length - 1];

function yearOf(month) {
  const start = +month.slice(0, 4) - (+month.slice(5) < META.yearStart ? 1 : 0);
  return start + "-" + (start + 1);
}

function usable(i) {
  const d = record(current);
  if (!d || d.ndvi[i] === null) return false;
  return isYear(current) ? d.nm[i] >= META.minMonths : d.cov[i] >= META.minCov;
}

function style(feature) {
  const i = feature.properties.i;
  return {
    fillColor: usable(i) ? ndviColor(record(current).ndvi[i] / 1000) : NO_DATA_COLOR,
    fillOpacity: usable(i) ? 0.75 : 0.45,
    color: "#555", weight: 0.3,
  };
}

function tooltip(p) {
  const d = record(current);
  if (!d) return "<b>" + p.c + "</b> – " + p.n;
  const v = d.ndvi[p.i];
  const ndvi = v === null ? "–" : (v / 1000).toFixed(3);
  const head = "<b>" + p.c + "</b> – " + p.n + "<br>Code IRIS : " + p.k;
  if (isYear(current)) {
    return head + "<br>NDVI moyen annuel : " + ndvi + "<br>Mois exploitables : " + d.nm[p.i] + " / 12";
  }
  return head + "<br>NDVI moyen : " + ndvi + "<br>Pixels (" + META.resolution + " m) : " + d.px[p.i] +
         "<br>Nuages : " + (100 - d.cov[p.i]) + " %";
}

const irisLayer = L.geoJSON(GEO, {
  style,
  onEachFeature: (f, layer) => {
    layer.bindTooltip(() => tooltip(f.properties), { sticky: true });
    layer.on("mouseover", () => layer.setStyle({ weight: 2, color: "#000" }));
    layer.on("mouseout", () => irisLayer.resetStyle(layer));
  },
}).addTo(map);

L.control.layers({ "Plan IGN": plan, "Photos aériennes (IGN)": ortho },
                 { "NDVI par IRIS": irisLayer }, { position: "topleft" }).addTo(map);

// ---- Year / month selectors, summary and CSV export ----
const panel = L.control({ position: "topright" });
panel.onAdd = () => {
  const div = L.DomUtil.create("div", "panel");
  div.innerHTML =
    "<h1>NDVI moyen par IRIS – Île-de-France</h1>" +
    "<label for='year'>Année (oct.–sept.)</label><select id='year'></select><br>" +
    "<label for='month'>Période</label><select id='month'></select><br>" +
    "<button id='export'>Exporter la période en CSV</button> " +
    "<button id='export-all'>Tout exporter (ZIP)</button>" +
    "<div class='info' id='info'></div>";
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};
panel.addTo(map);

const yearSelect = document.getElementById("year");
const monthSelect = document.getElementById("month");
const years = [...new Set(months.map(yearOf))];
yearSelect.innerHTML = years.map(y => "<option>" + y + "</option>").join("");

// "Moyenne annuelle", then the 12 months of the year, October first
function fillMonths(year) {
  const options = ["<option value='" + year + "'" + (ANNUAL[year] ? "" : " disabled") +
                   ">Moyenne annuelle</option>"];
  for (let k = 0; k < 12; k++) {
    const m = (META.yearStart - 1 + k) % 12;
    const y = +year.slice(0, 4) + (META.yearStart - 1 + k >= 12 ? 1 : 0);
    const key = y + "-" + String(m + 1).padStart(2, "0");
    options.push("<option value='" + key + "'" + (DATA[key] ? "" : " disabled") + ">" +
                 MONTH_NAMES[m] + " " + y + "</option>");
  }
  monthSelect.innerHTML = options.join("");
}

function show(key) {
  current = key;
  yearSelect.value = isYear(key) ? key : yearOf(key);
  fillMonths(yearSelect.value);
  monthSelect.value = key;
  irisLayer.setStyle(style);
  const n = GEO.features.filter(f => usable(f.properties.i)).length;
  let text = n + " / " + GEO.features.length + " IRIS exploitables";
  if (isYear(key)) {
    text += "<br>Moyenne des NDVI mensuels d'octobre à septembre, sur les mois où l'IRIS a moins de " +
      (100 - META.minCov) + " % de nuages. En gris : moins de " + META.minMonths + " mois exploitables.";
  } else {
    const cov = DATA[key].cov.reduce((a, b) => a + b, 0) / DATA[key].cov.length;
    text += " · " + Math.round(100 - cov) + " % de nuages en moyenne<br>Image Sentinel-2 la moins " +
      "nuageuse du mois, " + META.resolution + " m. En gris : plus de " + (100 - META.minCov) +
      " % de nuages sur l'IRIS.";
  }
  document.getElementById("info").innerHTML = text;
}

// Changing year keeps the same kind of view: annual mean, or the same calendar month
yearSelect.addEventListener("change", () => {
  const year = yearSelect.value;
  let target = year;
  if (!isYear(current)) {
    const sameMonth = (+current.slice(5) < META.yearStart ? +year.slice(5) : +year.slice(0, 4)) +
                      current.slice(4);
    if (DATA[sameMonth]) target = sameMonth;
  }
  const inYear = months.filter(m => yearOf(m) === year);
  show(record(target) ? target : inYear[inYear.length - 1]);
});
monthSelect.addEventListener("change", () => show(monthSelect.value));

function csvField(value) {
  const s = String(value);
  return /[;"\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function csvHeader(key) {
  return ["commune", "iris", "code_iris", "ndvi_moyen",
          ...(isYear(key) ? ["mois_exploitables"] : ["nombre_pixels", "nuages_pct"])];
}

function csvRows(key) {
  const d = record(key);
  return GEO.features.map(f => {
    const p = f.properties, v = d.ndvi[p.i];
    // French Excel conventions: ";" separator, decimal comma
    const ndvi = v === null ? "" : (v / 1000).toFixed(3).replace(".", ",");
    return [p.c, p.n, p.k, ndvi, ...(isYear(key) ? [d.nm[p.i]] : [d.px[p.i], 100 - d.cov[p.i]])];
  });
}

function toCsv(rows) {
  return "\\ufeff" + rows.map(r => r.map(csvField).join(";")).join("\\r\\n");
}

function save(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.getElementById("export").addEventListener("click", () => {
  const csv = toCsv([csvHeader(current), ...csvRows(current)]);
  save(new Blob([csv], { type: "text/csv;charset=utf-8" }), "ndvi_iris_idf_" + current + ".csv");
});

// One CSV per month and per year, plus all months (resp. years) stacked in a single file
// with a "mois" (resp. "annee") column
document.getElementById("export-all").addEventListener("click", async () => {
  const button = document.getElementById("export-all");
  button.disabled = true;
  button.textContent = "Préparation du ZIP…";
  try {
    const zip = new JSZip();
    for (const [keys, column, stackedName] of [[months, "mois", "tous_les_mois"],
                                               [yearKeys, "annee", "toutes_les_annees"]]) {
      if (!keys.length) continue;
      const stacked = [[column, ...csvHeader(keys[0])]];
      for (const key of keys) {
        const rows = csvRows(key);
        zip.file("ndvi_iris_idf_" + key + ".csv", toCsv([csvHeader(key), ...rows]));
        for (const r of rows) stacked.push([key, ...r]);
      }
      zip.file("ndvi_iris_idf_" + stackedName + ".csv", toCsv(stacked));
    }
    const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
    save(blob, "ndvi_iris_idf_" + months[0] + "_" + months[months.length - 1] + ".zip");
  } finally {
    button.disabled = false;
    button.textContent = "Tout exporter (ZIP)";
  }
});

// ---- Legend ----
const legend = L.control({ position: "bottomright" });
legend.onAdd = () => {
  const div = L.DomUtil.create("div", "panel legend");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(t => (META.vmin + t * (META.vmax - META.vmin)).toFixed(1));
  div.innerHTML = "<b>NDVI moyen</b>" +
    "<div class='bar' style='background:linear-gradient(to right," + STOPS.join(",") + ")'></div>" +
    "<div class='ticks'>" + ticks.map(t => "<span>" + t + "</span>").join("") + "</div>" +
    "<div class='nodata'><span class='swatch'></span>Nuages / pas de donnée</div>";
  return div;
};
legend.addTo(map);

if (months.length) show(current);
else document.getElementById("info").textContent = "Aucun mois téléchargé pour l'instant.";
</script>
</body>
</html>
"""


def main():
    iris = load_iris_idf()
    print(f"{len(iris)} IRIS en Île-de-France")
    if "--map-only" not in sys.argv:
        update_monthly_stats(iris)
    monthly = load_monthly_stats(iris)
    annual = annual_stats(monthly)
    save_annual_stats(annual)
    make_map(iris, monthly, annual, OUT_HTML)


if __name__ == "__main__":
    main()
