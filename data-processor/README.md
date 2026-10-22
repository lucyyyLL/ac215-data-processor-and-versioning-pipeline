# Green Path Data Processor

Collects Mapillary streetview metadata and JPG images, assigns them to H3 R10
cells, and selects up to four images per cell using heading diversity and recency.
Settings come from the repository's `configs/data.yaml`.

## Start the container

Use a Linux/WSL environment with Docker and FUSE support. Check the bucket,
project and secrets directory configured in `docker-shell.sh` before starting.
The secrets directory must contain `data-service-account.json` and
`mapillary_token.txt`; do not commit these files.

From the repository root:

```bash
cd data-processor
bash docker-shell.sh
```

The Docker build installs locked dependencies using `uv sync --frozen`.
The entrypoint loads `MAPILLARY_TOKEN`, authenticates with the service account,
mounts the configured bucket at `/mnt/gcs_data`, and activates the Python environment.
Files written under this mount are stored in GCS.

## Run streetview collection

Inside the container, use the module directly:

```bash
cd /app/data-processor
python -m src.streetview
```

This uses the configured Boston bounding box and sample size and writes to
`/mnt/gcs_data/datasets/boston/`. A configured sample size of `0` collects all
eligible cells. To collect a smaller area directly into this production dataset,
edit the bounding box or sample size in `configs/data.yaml`, then run without CLI overrides.

For a small test:

```bash
python -m src.streetview \
  --bbox -71.10 42.35 -71.09 42.36 \
  --sample-size 5
```

This collects up to five eligible cells under
`/mnt/gcs_data/datasets/_test/boston/`. Either `--bbox` or `--sample-size`
defaults to test storage; explicitly targeting the production root with these
overrides is rejected. Bounding-box order is west, south, east, north.

Use `--dataset-root /path/to/another/city-root` for a custom output directory,
`--config /path/to/data.yaml` for another configuration, or `--help` to list arguments.

## Run NAIP extraction

NAIP reads the existing `interim/cells.parquet` from Streetview and leaves all
Streetview tables unchanged. Inside the container:

```bash
cd /app/data-processor
python -m src.naip --test  # Existing cells in datasets/_test/boston
python -m src.naip        # Existing cells in datasets/boston
```

The default crop is 154m square, resampled to a 256 x 256 RGB PNG. Missing black
pixels above 5% make a tile unusable. The index retains missing/failed results for retry.
The existing extraction-settings hash, including its library-version identity
inputs, determines the Tile Set ID; saved paths remain compatible. Reruns reuse
indexed usable PNGs when their files exist, without rereading or rehashing them.
`--config` and `--dataset-root` are also supported; `--test` rejects the production root.

The global `naip.source_snapshot_id` is currently unset. Each cell's assigned item
is retained for retries, and unsigned asset paths identify its source. Remote ETags
are no longer requested; new `source_asset_checksum` values are null while the
column remains compatible with old data. Use a new source/version setting for
intentional imagery changes. This does not freeze the external catalog.

## Run OSM and WorldCover extraction

```bash
cd /app/data-processor
python -m src.osm_features --test  # Existing five-cell test dataset
python -m src.osm_features        # Production cells
```

This reads all rows from Streetview's `interim/cells.parquet`, independently of
NAIP usability. It writes
`processed/osm_features.parquet` and `processed/osm_features.meta.json` without
changing Streetview or NAIP outputs. OSM PBF snapshots are retained under
`raw/osm/<region>_<sha256>.osm.pbf` with download metadata.

`bike_lane_m` counts dedicated lane/track road tags; separately mapped cycleways
remain in `road_path_m`. `maxspeed_avg` is in km/h. WorldCover fractions exclude
class 0; all-NoData crops remain missing. Matching declared settings, CRS and PBF
path allow existing cells to be skipped without querying WorldCover. Changes trigger a full
recomputation of the current cells table. Update `features.processing_version`
when changing feature definitions in code. Used WorldCover item IDs remain recorded.

Set `features.osm.snapshot_path` to an existing local/GCSFuse PBF path (absolute
or relative to the city root). Otherwise the previously recorded snapshot is
reused for the same URL, or a new snapshot is downloaded. SHA-256 is computed
only during download; reruns do not verify source bytes or query ETags.

## Outputs and repeated runs

Paths relative to the selected city root:

| Path | Contents |
| --- | --- |
| `raw/mapillary/metadata/tile_z14_<x>_<y>.parquet` | Cached Mapillary metadata |
| `raw/mapillary/images/<image_id>.jpg` | Downloaded streetview images |
| `interim/grid.parquet` | Collected H3 grid |
| `interim/cells.parquet` | Retained cells and image statistics |
| `interim/streetview.parquet` | Selected images, relative paths and download status |
| `processed/tiles/<tile_set_id>/<cell_id>.png` | NAIP RGB crops |
| `processed/naip_tiles.parquet` | NAIP results keyed by `(cell_id, tile_set_id)` |
| `processed/osm_features.parquet` | OSM and WorldCover features keyed by `cell_id` |
| `processed/osm_features.meta.json` | Feature configuration and source provenance |

Repeated runs reuse existing metadata caches and nonempty JPGs, merge
records by image/cell IDs, and preserve other collection areas. `n_images` counts
successful downloads; failed image records can remain with `downloaded=False`.

Inspect test outputs inside the container:

```bash
ls /mnt/gcs_data/datasets/_test/boston/interim/
ls /mnt/gcs_data/datasets/_test/boston/raw/mapillary/images/
python -c "import pandas as pd; print(pd.read_parquet('/mnt/gcs_data/datasets/_test/boston/interim/cells.parquet').head())"
```

`src/config.py` handles YAML loading and dataset roots; `src/streetview.py` and
`src/naip.py` implement their respective collection CLIs. `main.py` and `storage.py` are currently empty.
Labeling, collection-history/manifest publication and DVC integration
are not implemented in this module yet.

## Coding standard

Follow the project [AGENTS.md](../AGENTS.md): use direct procedural pipelines,
meaningful functions, minimal source metadata and lightweight incremental merges.
Keep essential download recovery and test/production guards; let unexpected
configuration and storage errors fail. Trust completed outputs rather than adding
verification reads or API calls. Preserve schemas, algorithms and existing Tile Set
paths, and report the exact regression coverage rather than assuming equivalence.
