"""Collect Mapillary streetview: python -m src.streetview --sample-size 5."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import h3
import mapbox_vector_tile
import mercantile
import numpy as np
import pandas as pd
import requests

from src.config import CONFIG_PATH, get_dataset_root, load_config
from src.storage import read_existing_parquet, save_parquet


META_COLS = ["image_id", "lon", "lat", "captured_at_ms", "compass_raw",
             "is_pano", "sequence", "quality"]
GRID_COLS = ["cell_id", "city", "h3_index", "h3_res", "lat", "lon"]
CELL_COLS = [*GRID_COLS, "n_images_selected", "n_images", "n_quadrants",
             "capture_min", "capture_max", "n_images_filtered", "updated_at"]
SV_COLS = ["image_id", "cell_id", "h3_index", "lat", "lon", "captured_at",
           "compass", "quadrant", "is_pano", "sequence", "image_path", "downloaded"]


def request_bytes(url, params=None, *, timeout=60, tries=4, vector=False):
    """Retain the notebook's exponential backoff and fail-fast auth errors."""
    for attempt in range(tries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if vector and response.status_code in (204, 404):
                return b""
            if response.status_code in (400, 401, 403):
                response.raise_for_status()
            if response.status_code == 200:
                return response.content
            if not vector and response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
        except requests.RequestException as error:
            if error.response is not None and error.response.status_code in (400, 401, 403):
                raise
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt)
    response.raise_for_status()
    raise requests.RequestException("Empty response after retries")


def decode_tile(content, tile, layer_name):
    if not content:
        return pd.DataFrame(columns=META_COLS)
    layer = mapbox_vector_tile.decode(content, default_options={"y_coord_down": True}).get(layer_name)
    features = [f for f in (layer["features"] if layer else [])
                if f["geometry"]["type"] == "Point"]
    if not features:
        return pd.DataFrame(columns=META_COLS)
    extent = layer.get("extent", 4096)
    xy = np.array([f["geometry"]["coordinates"] for f in features], dtype=float)
    bounds = mercantile.xy_bounds(tile)
    mx = bounds.left + xy[:, 0] / extent * (bounds.right - bounds.left)
    my = bounds.top - xy[:, 1] / extent * (bounds.top - bounds.bottom)
    radius = 6378137.0
    props = [f["properties"] for f in features]
    return pd.DataFrame({
        "image_id": [str(p["id"]) for p in props],
        "lon": np.degrees(mx / radius),
        "lat": np.degrees(2 * np.arctan(np.exp(my / radius)) - np.pi / 2),
        "captured_at_ms": [p.get("captured_at") for p in props],
        "compass_raw": [p.get("compass_angle") for p in props],
        "is_pano": [bool(p.get("is_pano")) for p in props],
        "sequence": [p.get("sequence_id") for p in props],
        "quality": [p.get("quality_score") for p in props],
    })


def collect_metadata(config, root, bbox, token):
    settings = config["mapillary"]["metadata"]
    parts = []
    for tile in mercantile.tiles(*bbox, zooms=[settings["tile_zoom"]]):
        coords = dict(z=tile.z, x=tile.x, y=tile.y)
        target = root / config["outputs"]["mapillary_metadata"].format(**coords)
        if target.exists():
            parts.append(pd.read_parquet(target))
            continue
        content = request_bytes(settings["url_template"].format(**coords),
                                {"access_token": token}, timeout=180, vector=True)
        frame = decode_tile(content, tile, settings["layer"]).drop_duplicates("image_id")
        save_parquet(frame, target)
        parts.append(frame)
    return pd.concat(parts, ignore_index=True).drop_duplicates("image_id") if parts else pd.DataFrame(columns=META_COLS)


def filter_metadata(raw, bbox, config):
    df = raw.copy()
    for column in ["lon", "lat", "captured_at_ms", "compass_raw", "quality"]:
        df[column] = pd.to_numeric(df[column])
    df["is_pano"] = df.is_pano.fillna(False).astype(bool)
    df["captured_at"] = pd.to_datetime(df.captured_at_ms, unit="ms", utc=True)
    df["compass"] = df.compass_raw
    lo0, la0, lo1, la1 = bbox
    filters = config["mapillary"]["filters"]
    df = df[df.lon.between(lo0, lo1) & df.lat.between(la0, la1)]
    df = df[(df.captured_at.dt.year >= filters["min_year"]) &
            ~df.captured_at.dt.month.isin(filters["exclude_months"])]
    if not filters["allow_pano"]:
        df = df[~df.is_pano]
    if filters["min_quality"] > 0:
        df = df[df.quality.fillna(0) >= filters["min_quality"]]
    if filters["require_heading_for_non_pano"]:
        df = df[df.compass.notna() | df.is_pano]
    df["compass"] = df.compass.fillna(0.0) % 360
    df["h3_index"] = [h3.latlng_to_cell(lat, lon, config["spatial"]["h3_resolution"])
                      for lat, lon in zip(df.lat, df.lon)]
    return df


def build_grid(bbox, config):
    lo0, la0, lo1, la1 = bbox
    polygon = h3.LatLngPoly([(la0, lo0), (la0, lo1), (la1, lo1), (la1, lo0)])
    spatial, city = config["spatial"], config["city"]
    rows = []
    for index in sorted(h3.polygon_to_cells(polygon, spatial["h3_resolution"])):
        lat, lon = h3.cell_to_latlng(index)
        rows.append([spatial["cell_id_template"].format(city_prefix=city["prefix"], h3_index=index),
                     city["name"], index, spatial["h3_resolution"], lat, lon])
    return pd.DataFrame(rows, columns=GRID_COLS)


def pick_diverse(group, maximum, quadrant_degrees=90):
    group = group.sort_values("captured_at", ascending=False).copy()
    group["quadrant"] = (group.compass // quadrant_degrees).astype(int) % 4
    chosen = group.groupby("quadrant").head(1).sort_values("captured_at", ascending=False).head(maximum)
    if len(chosen) < maximum:
        chosen = pd.concat([chosen, group.drop(chosen.index).head(maximum - len(chosen))])
    return chosen


def download_image(row, root, config, token):
    target = root / row.image_path
    if target.is_file() and target.stat().st_size:
        return True
    if not config["mapillary"]["images"]["download"]:
        return False
    settings = config["mapillary"]
    field = settings["images"]["thumbnail_field"]
    try:
        metadata = json.loads(request_bytes(f'{settings["graph_api"]}/{row.image_id}',
                                           {"access_token": token, "fields": field}))
        url = metadata.get(field)
        content = request_bytes(url) if url else None
    except requests.RequestException:
        return False
    if not content:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".jpg.part")
    temporary.write_bytes(content)
    temporary.replace(target)
    return True


def select_images(candidates, grid, config, sample_size):
    sampling = config["mapillary"]["sampling"]
    candidates = candidates.drop_duplicates("image_id", keep="last").copy()
    candidates["captured_at"] = pd.to_datetime(candidates.captured_at, utc=True)
    groups = [pick_diverse(group, sampling["max_images_per_cell"], sampling["quadrant_degrees"])
              for _, group in candidates.groupby("h3_index")]
    selected = pd.concat(groups, ignore_index=True) if groups else candidates.assign(quadrant=pd.Series(dtype="int64"))
    stats = selected.groupby("h3_index").agg(
        n_images_selected=("image_id", "size"), n_quadrants=("quadrant", "nunique"),
        capture_min=("captured_at", "min"), capture_max=("captured_at", "max"))
    cells = pd.concat([grid, stats.reindex(grid.h3_index).reset_index(drop=True)], axis=1)
    cells = cells[cells.n_images_selected >= sampling["min_images_per_cell"]].copy()
    if sample_size and sample_size < len(cells):
        cells = cells.sample(n=sample_size, random_state=sampling["seed"])
    counts = ["n_images_selected", "n_quadrants"]
    cells[counts] = cells[counts].astype("int64")
    cells["n_images_filtered"] = cells.h3_index.map(candidates.groupby("h3_index").size())
    selected = selected[selected.h3_index.isin(cells.h3_index)].copy()
    selected["cell_id"] = pd.Series([config["spatial"]["cell_id_template"].format(
        city_prefix=config["city"]["prefix"], h3_index=index) for index in selected.h3_index],
        index=selected.index, dtype="object")
    selected["image_path"] = [config["outputs"]["streetview_images"].format(image_id=i) for i in selected.image_id]
    return cells, selected


def collect(config, root, bbox, sample_size, token):
    """Merge one collection area, preserving records in other city areas."""
    root = Path(root)
    outputs, sampling = config["outputs"], config["mapillary"]["sampling"]
    minimum = sampling["min_images_per_cell"]
    raw = collect_metadata(config, root, bbox, token)
    filtered = filter_metadata(raw, bbox, config)
    grid = build_grid(bbox, config)

    old_sv = read_existing_parquet(root / outputs["streetview"], SV_COLS)
    old_cells = read_existing_parquet(root / outputs["cells"], GRID_COLS)
    # Include previous selected images in overlapping cells, even outside this bbox.
    previous = old_sv[old_sv.h3_index.isin(grid.h3_index)]
    candidates = filtered if previous.empty else pd.concat([previous, filtered], ignore_index=True)
    cells, selected = select_images(candidates, grid, config, sample_size)
    with ThreadPoolExecutor(max_workers=config["mapillary"]["images"]["workers"]) as executor:
        selected["downloaded"] = list(executor.map(
            lambda row: download_image(row, root, config, token), selected.itertuples()))
    selected["downloaded"] = selected.downloaded.astype(bool)
    successes = selected[selected.downloaded].groupby("cell_id").size()
    keep = (successes[successes >= minimum].index
            if config["mapillary"]["images"]["download"] else cells.cell_id)
    selected = selected[selected.cell_id.isin(keep)].copy()
    # If an update cannot meet the minimum, leave that cell's previous records intact.
    remaining = old_sv[~old_sv.cell_id.isin(keep)]
    sv = selected[SV_COLS].copy() if remaining.empty else pd.concat([remaining, selected[SV_COLS]], ignore_index=True)
    sv = sv.drop_duplicates("image_id", keep="last").sort_values(["cell_id", "image_id"])

    cells = cells[cells.cell_id.isin(keep)].copy()
    cells["n_images"] = cells.cell_id.map(successes).fillna(0).astype("int64")
    for name in ["capture_min", "capture_max"]:
        cells[name] = cells[name].dt.strftime("%Y-%m")
    cells["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = cells[CELL_COLS]
    remaining = old_cells[~old_cells.cell_id.isin(keep)]
    cells = cells if remaining.empty else pd.concat([remaining, cells], ignore_index=True)
    cells = cells.drop_duplicates("cell_id", keep="last").sort_values("cell_id")
    sv["captured_at"] = pd.to_datetime(sv.captured_at, utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    grid_path = root / outputs["grid"]
    if grid_path.exists():
        grid = pd.concat([pd.read_parquet(grid_path), grid], ignore_index=True).drop_duplicates("cell_id", keep="last")
    for name, frame in [("grid", grid), ("cells", cells), ("streetview", sv)]:
        save_parquet(frame, root / outputs[name])
    print(f"{root}: {len(grid)} grid cells, {len(cells)} retained cells, {len(sv)} image records")
    return grid, cells, sv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--sample-size", type=int, help="0 uses all eligible cells")
    parser.add_argument("--dataset-root", type=Path, help="Alternative city root; required overrides default to datasets/_test/<city>")
    args = parser.parse_args()
    config = load_config(args.config)
    if config["spatial"]["h3_resolution"] != 10 or config["mapillary"]["metadata"]["tile_zoom"] != 14:
        parser.error("Green Path requires H3 R10 and Mapillary zoom 14")
    if args.sample_size is not None and args.sample_size < 0:
        parser.error("--sample-size must be nonnegative")
    bbox = args.bbox or config["city"]["bbox"]
    if not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
        parser.error("--bbox must be WEST SOUTH EAST NORTH")
    override = args.bbox is not None or args.sample_size is not None
    root = get_dataset_root(config, testing=override, dataset_root=args.dataset_root)
    if override and root.resolve() == get_dataset_root(config).resolve():
        parser.error("Test overrides must use a different dataset root")
    sample_size = args.sample_size if args.sample_size is not None else config["mapillary"]["sampling"]["sample_size"]
    collect(config, root, bbox, sample_size, os.environ["MAPILLARY_TOKEN"])


if __name__ == "__main__":
    main()
