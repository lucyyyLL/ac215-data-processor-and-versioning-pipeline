# Green Path Data Reference

## 1. GCS Directory Structure

One continuously expanding dataset per city; the following is the proposed container layout.

```text
gs://<bucket>/
+-- datasets/
|   +-- boston/
|   |   +-- raw/
|   |   |   +-- mapillary/
|   |   |   |   +-- metadata/tile_z{z}_{x}_{y}.parquet
|   |   |   |   +-- images/{image_id}.jpg
|   |   |   +-- osm/
|   |   |       +-- {osm_region}_{osm_snapshot_sha256}.osm.pbf
|   |   |       +-- {osm_region}_{osm_snapshot_sha256}.osm.pbf.meta.json
|   |   +-- interim/
|   |   |   +-- grid.parquet
|   |   |   +-- cells.parquet
|   |   |   +-- streetview.parquet
|   |   +-- processed/
|   |   |   +-- tiles/{tile_set_id}/{cell_id}.png
|   |   |   +-- naip_tiles.parquet
|   |   |   +-- osm_features.parquet
|   |   |   +-- labels/
|   |   |   |   +-- streetview_scores.parquet
|   |   |   |   +-- cell_scores.parquet
|   |   |   +-- dataset.parquet
|   |   +-- collection_history.parquet
|   |   +-- manifest.json
|   +-- new_york/... (same layout)
+-- dvc_store/
```

`raw` preserves sources, `interim` holds selected cells/images, and `processed` holds crops, features and future labels/training data.
Paths in tables are relative to `gs://<bucket>/datasets/<city>/`; the city-root URI is recorded in `manifest.json`.
No acquisition-run or H3-resolution folders are used. `run_id` is metadata only, never a file-path component.

## 2. Data Files & Schemas

The table specifies proposed paths. Existing notebook schemas are retained unless marked **new** or **future**.
IDs/paths are strings, counts and pixel sizes integers, measurements/scores float64, flags bool; timestamps are UTC ISO strings.
`?` means nullable. Physical Parquet types remain inferred from notebook code, pending inspection of real outputs.

| File (format; producer) | Row / primary key | Important fields |
| --- | --- | --- |
| `raw/mapillary/metadata/tile_z{z}_{x}_{y}.parquet` (Parquet; 01) | Image within a tile / `image_id` per file | `lon`, `lat`, `captured_at_ms?`, `compass_raw?`, `quality?`, `is_pano`, `sequence?`; duplicates across files allowed |
| `interim/grid.parquet` (Parquet; 01) | H3 cell / `cell_id` | `city`, `h3_index`, `h3_res=10`, center `lat`, `lon`; no persisted coverage counts |
| `interim/cells.parquet` (Parquet; 01) | Retained cell / `cell_id` | Grid fields; `n_images_filtered`, `n_images_selected`, `n_quadrants`, `n_images`, `capture_min/max` (YYYY-MM), `updated_at` |
| `interim/streetview.parquet` (Parquet; 01) | Selected image / `image_id` | `cell_id`, `h3_index`, image `lat/lon`, `captured_at`, `compass`, `quadrant`, `is_pano`, `sequence?`, `image_path`, `downloaded` |
| `processed/naip_tiles.parquet` (Parquet; **new**, refactored 02) | Cell/Tile Set attempt / `(cell_id, tile_set_id)` | `h3_index`, `tile_path?`, `tile_px`, `ground_m`, `resampling_method`, `naip_item?`, `naip_year?`, `nodata_frac?`, `gsd?`, `tile_ok`, `error?`, `updated_at`; provenance fields below |
| `processed/osm_features.parquet` (Parquet; 03) | Feature-bearing cell / `cell_id` | `h3_index`, `road_{major,secondary,local,path,other,total}_m`, `bike_lane_m`, `lanes_avg?`, `maxspeed_avg?`, `intersections`, `poi_count`, `osm_{water,green,building}_frac`, `wc_<class>_frac?`, `wc_dominant?`, `updated_at` |
| `processed/labels/streetview_scores.parquet` (Parquet; **future**) | Image/attribute/label run / `(image_id, attribute, label_run_id)` | `cell_id`, `model_id/version`, `score?`, `parameters_sha256`, `created_at`, `status`, `error?` |
| `processed/labels/cell_scores.parquet` (Parquet; **future**) | Cell/attribute/label run / `(cell_id, attribute, label_run_id)` | `source_label_run_id`, `model_id/version`, `score?`, `aggregation_method`, `n_images_scored`, `parameters_sha256`, `created_at` |
| `processed/dataset.parquet` (Parquet; **future**) | Selected tile/attribute/label run / `(cell_id, tile_set_id, attribute, label_run_id)` | `city`, `h3_index`, `tile_path`, `naip_item/year`, `score`, `model_id/version`, `n_images_scored`, OSM/WorldCover fields, `source_dataset_version`, `assembly_run_id` |
| `collection_history.parquet` (Parquet; **new**) | Acquisition attempt / `run_id` | Start/end UTC, status, city/coverage, config hash and resolved parameters, code version, source/output references, Tile Sets, counts/errors |
| `manifest.json` (JSON; **new**) | Current published city state / city-root URI | Source/config references, Tile Set definitions, data versions, artifact paths/generations/checksums, history reference, label/training selections, validation |
| `raw/osm/{osm_region}_{osm_snapshot_sha256}.osm.pbf` (+ `.meta.json`; PBF/JSON; proposed 03 storage) | Source snapshot / full SHA-256 | Original PBF bytes; sidecar records source URL/date, download time, hash and size |
| Image objects (JPEG; 01 / PNG; 02) | Image / `image_id` or `(cell_id, tile_set_id)` | Streetview API image bytes; RGB NAIP crop, default 256px over 154m |

NAIP provenance fields: `config_sha256`, `source_snapshot_id`, `source_asset_identity?`, `source_asset_checksum?`, `tile_sha256?`, `run_id`; successful reusable tiles require source/output identities and hashes.
WorldCover classes: tree, shrub, grass, crop, built, bare, snow, water, wetland, mangrove, moss; configuration lists their codes.
Current notebooks use local storage by default: 01 writes grid under `raw/grid` and cells/streetview under `processed`; 02 appends NAIP fields to cells and writes ungrouped crops; 03 writes the feature table.
They do not implement Tile Sets, the separate NAIP index, city-root manifest or collection-history table; labeling/training are also unimplemented. Existing step manifests are not the proposed publication contract.
Notebook 03 currently caches the PBF locally in GCS mode; preserving snapshot-named PBFs in GCS is proposed.

## 3. Data Relationships

- `h3_index` is the global H3 string at fixed resolution 10; `cell_id = <CITY_PREFIX>_<h3_index>` (Boston: `BOS`). Both stay stable across experiments.
- Grid contains cells; selected cells reference matching `(cell_id, h3_index)` pairs. Many streetview `image_id` values map to one cell.
- One cell may have multiple NAIP tiles, one per `tile_set_id`; `(cell_id, tile_set_id)` uniquely identifies the current index row, including failures.
- OSM/WorldCover features join by matching cell/H3. Their default 154m square footprint is independent of NAIP Tile Sets; feature snapshot changes are versioned separately.
- Image scores join streetview by `image_id` and model/label version; cell scores aggregate valid image scores and join by `cell_id`, attribute and label run.
- Training explicitly selects a Tile Set, successful tile rows, cell scores/model version and a feature snapshot. Aerial tile is X; cell visual score is Y. Do not implicitly cross-join every Tile Set.
- Multi-city assembly carries `city`, city-root/source-version references and relative paths so identical filenames or global H3 IDs remain unambiguous.

## 4. Naming & Versioning Rules

Streetview: `raw/mapillary/images/{image_id}.jpg`, reusable across Tile Sets. Resolution/format changes require a separately identified asset variant; preserve the original and record the scoring input variant/hash.
NAIP: `processed/tiles/{tile_set_id}/{cell_id}.png`; Tile Set names use `naip_<ground_m>m_<tile_px>px_<version>_<hash12>`.
The same Tile Set always means the same effective extraction configuration and compatible pinned source version; detailed hashing rules are in YAML.
Changed content-affecting parameters or selected imagery for an existing cell require a new Tile Set. Different Tile Sets coexist; existing validated tiles must not be overwritten or silently reused on mismatch.
OSM retains the Geofabrik source URL but stores each downloaded PBF under its full content hash, never a mutable stored `latest` name.
Scores remain in separate tables: append a new label run/model version rather than replacing previous scores. DVC retains historical table and dataset versions.

## 5. Incremental Collection & Provenance

Expand city coverage by merging new cells/images and adding missing tiles under the same configuration and compatible pinned source version; reuse validated existing assets without downloading them again.
A source snapshot defines a reproducible release/catalog cutoff and immutable asset policy, independent of coverage. Per-cell source assignments are recorded in the NAIP index and may only be extended to new cells.
The growing assignment inventory has its own publication checksum; it is not itself the fixed source-version identity. A changed existing assignment requires a new source identity/Tile Set.
Check recorded configuration, source identity, output checksum and object existence before reuse; retry missing/invalid assets without silently replacing conflicting validated content.
Append acquisition attempts to `collection_history.parquet` using `run_id` as a column; include failures and resolved non-secret parameters. Publish the city-root `manifest.json` last after consistent index/history updates.
The manifest describes current sources, effective configurations, Tile Sets, feature/label/training versions and artifact checksums/generations. DVC preserves prior published states; run IDs do not partition storage.
Only successful downloads/tiles and valid scores enter training. Current streetview rows may include failed downloads, and `n_images` counts rows; check `downloaded` and `tile_ok`.
Physical types and the existing WorldCover dominant-class calculation need verification; label scales/models/aggregation remain explicit placeholders. Existing feature semantics stay unchanged.

## 6. GCS & DVC Workflow

- **Data Collection Container:** consumes YAML/environment settings, collects incrementally, writes outputs directly to GCS, updates history and publishes the manifest. Only temporary scratch (including pyrosm's PBF) is local; no persistent local dataset is required.
- **GCS storage:** holds one expanding dataset per city, immutable source snapshots and coexisting Tile Sets; published tables describe the current state. Credentials and expiring signed URLs are excluded from provenance.
- **DVC Container:** versions published dataset bytes, indexes, history and manifest in the separate `gs://<bucket>/dvc-store/` remote, using ephemeral staging if needed. The manifest records provenance; DVC makes historical states reproducible.
- **GitHub:** stores code/configuration, DVC metadata and version references, not bulk data or secrets. DVC snapshots identify the corresponding code/configuration revision.
