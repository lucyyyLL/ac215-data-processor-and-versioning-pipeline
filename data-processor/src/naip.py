"""Crop NAIP imagery for existing Streetview cells: python -m src.naip --test."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
from importlib.metadata import version
import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from PIL import Image
import planetary_computer
from pyproj import Transformer
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from shapely.geometry import Point, box, shape

from src.config import CONFIG_PATH, get_dataset_root, load_config


GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="3",
                GDAL_HTTP_RETRY_DELAY="2", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
KEY = ["cell_id", "tile_set_id"]
COLUMNS = ["cell_id", "h3_index", "tile_set_id", "tile_path", "tile_px", "ground_m",
           "resampling_method", "naip_item", "naip_year", "nodata_frac", "gsd",
           "tile_ok", "error", "updated_at", "config_sha256", "source_snapshot_id",
           "source_asset_identity", "source_asset_checksum", "tile_sha256", "run_id"]


def get_tile_set(config):
    """Keep the existing hash recipe so saved Tile Set paths remain stable."""
    settings = config["naip"]
    effective = {}
    for field in settings["tile_set_identity"]["effective_config_fields"]:
        value = config
        for part in field.split("."):
            value = value[part]
        effective[field] = value
    if effective["naip.effective_dependency_versions"] is None:
        effective["naip.effective_dependency_versions"] = {
            name: version(name) for name in ["rasterio", "pyproj", "pillow", "numpy", "shapely"]}
        effective["naip.effective_dependency_versions"]["gdal"] = rasterio.__gdal_version__
    for field in ["naip.ground_m", "naip.tile_px"]:
        if float(effective[field]).is_integer():
            effective[field] = int(effective[field])
    digest = hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    tile_set_id = settings["tile_set_identity"]["id_template"].format(
        ground_m=effective["naip.ground_m"], tile_px=effective["naip.tile_px"],
        tile_set_version=settings["tile_set_version"],
        config_hash12=digest[:settings["tile_set_identity"]["hash_prefix_length"]])
    return tile_set_id, digest


def item_year(item):
    return int(item.properties["naip:year"] if "naip:year" in item.properties else item.datetime.year)


def pick_item(lon, lat, item_geoms, ground_m):
    half_lat = ground_m / 2 / 111_320
    half_lon = half_lat / np.cos(np.radians(lat))
    footprint = box(lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)
    full = [item for geometry, item in item_geoms if geometry.contains(footprint)]
    candidates = full or [item for geometry, item in item_geoms if geometry.contains(Point(lon, lat))]
    return max(candidates, key=lambda item: (item_year(item), item.datetime)) if candidates else None


def crop_tile(src, lon, lat, settings):
    x, y = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True).transform(lon, lat)
    row, col = src.index(x, y)
    width = settings["ground_m"] / abs(src.res[0])
    window = Window(col - width / 2, row - width / 2, width, width)
    tile = settings["tile"]
    arr = src.read(tile["bands"], window=window,
                   out_shape=(len(tile["bands"]), settings["tile_px"], settings["tile_px"]),
                   resampling=Resampling[settings["resampling"]],
                   boundless=tile["boundless"], fill_value=tile["fill_value"])
    image = np.moveaxis(arr, 0, -1).astype(tile["dtype"])
    return image, float((image.sum(axis=-1) == 0).mean())


def assign_naip_items(pending, existing, items, settings, tile_set_id, config_hash):
    geometries = [(shape(item.geometry), item) for item in items.values()]
    results, groups = [], {}
    run_id = uuid4().hex
    for cell in pending.itertuples():
        # Once assigned, retries use the original source rather than silently switching items.
        prior = existing.loc[cell.cell_id] if cell.cell_id in existing.index else None
        if prior is not None and pd.notna(prior.naip_item):
            item = items.get(prior.naip_item)
        else:
            item = pick_item(cell.lon, cell.lat, geometries, settings["ground_m"])
        row = dict.fromkeys(COLUMNS)
        row.update(cell_id=cell.cell_id, h3_index=cell.h3_index, tile_set_id=tile_set_id,
                   tile_px=settings["tile_px"], ground_m=float(settings["ground_m"]),
                   resampling_method=settings["resampling"], tile_ok=False,
                   updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   config_sha256=config_hash, source_snapshot_id=settings["source_snapshot_id"], run_id=run_id)
        if prior is not None:
            for field in ["naip_item", "naip_year"]:
                row[field] = prior[field]
        results.append(row)
        if item is None:
            row.update(error="Original NAIP item unavailable" if prior is not None else "No NAIP coverage",
                       naip_item=prior.naip_item if prior is not None else None)
            continue
        row.update(naip_item=item.id, naip_year=item_year(item))
        groups.setdefault(item.id, []).append((cell, row))
    return results, groups


def process_naip_tiles(groups, items, config, root, tile_set_id):
    settings, outputs = config["naip"], config["outputs"]
    with rasterio.Env(**GDAL_ENV):
        for item_id, group in groups.items():
            href = planetary_computer.sign(items[item_id].assets[settings["asset"]].href)
            identity = href.split("?", 1)[0]
            try:
                with rasterio.open(href) as src:
                    for cell, row in group:
                        row.update(source_asset_identity=identity, gsd=abs(src.res[0]))
                        try:
                            image, nodata = crop_tile(src, cell.lon, cell.lat, settings)
                        except rasterio.errors.RasterioError as error:
                            row["error"] = type(error).__name__
                            continue
                        relative = outputs["naip_images"].format(tile_set_id=tile_set_id, cell_id=cell.cell_id,
                                                               city_prefix=config["city"]["prefix"], h3_index=cell.h3_index)
                        buffer = io.BytesIO()
                        Image.fromarray(image).save(buffer, format=settings["tile"]["format"],
                                                    **settings["tile"]["encoder_options"])
                        content = buffer.getvalue()
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_suffix(".png.part")
                        temporary.write_bytes(content)
                        temporary.replace(target)
                        row.update(tile_path=relative, nodata_frac=nodata,
                                   tile_ok=nodata <= settings["tile"]["max_nodata_fraction"],
                                   tile_sha256=hashlib.sha256(content).hexdigest())
            except rasterio.errors.RasterioIOError as error:
                for _, row in group:
                    row["error"] = type(error).__name__


def collect(config, root):
    root = Path(root)
    settings, outputs = config["naip"], config["outputs"]
    cells = pd.read_parquet(root / outputs["cells"])
    tile_set_id, config_hash = get_tile_set(config)
    index_path = root / outputs["naip_tiles"]
    old = pd.read_parquet(index_path) if index_path.exists() else pd.DataFrame(columns=COLUMNS)
    existing = old[old.tile_set_id == tile_set_id].set_index("cell_id")
    if not existing.empty and existing.config_sha256.iloc[0] != config_hash:
        raise ValueError("Tile Set hash collision: choose a different tile_set_version")
    completed = existing[existing.tile_ok & existing.index.isin(cells.cell_id)]
    reusable = {row.Index for row in completed.itertuples() if (root / row.tile_path).is_file()}
    pending = cells[~cells.cell_id.isin(reusable)]
    if pending.empty:
        print(f"{tile_set_id}: {len(cells)} cells, all existing usable tiles reused")
        return old

    pad = settings["search_bbox_pad_degrees"]
    bbox = [pending.lon.min() - pad, pending.lat.min() - pad,
            pending.lon.max() + pad, pending.lat.max() + pad]
    catalog = pystac_client.Client.open(settings["stac_api"], modifier=planetary_computer.sign_inplace)
    items = list(catalog.search(collections=[settings["collection"]], bbox=bbox,
                                datetime=f'{settings["min_year"]}-01-01/{settings["search_end"]}').items())
    by_id = {item.id: item for item in items}
    results, groups = assign_naip_items(pending, existing, by_id, settings, tile_set_id, config_hash)
    process_naip_tiles(groups, by_id, config, root, tile_set_id)

    fresh = pd.DataFrame(results, columns=COLUMNS)
    fresh["naip_year"] = pd.array(fresh.naip_year, dtype="Int64")
    fresh["source_asset_checksum"] = fresh.source_asset_checksum.astype("string")
    combined = fresh if old.empty else pd.concat([old, fresh], ignore_index=True)
    combined = combined.drop_duplicates(KEY, keep="last").sort_values(KEY).reset_index(drop=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(index_path, index=False)
    print(f"{root}: {tile_set_id}, {len(reusable)} reused, {int(fresh.tile_ok.sum())}/{len(fresh)} new usable tiles")
    return combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--test", action="store_true", help="Use the existing Streetview test dataset")
    parser.add_argument("--dataset-root", type=Path, help="Alternative city dataset root")
    args = parser.parse_args()
    config = load_config(args.config)
    root = get_dataset_root(config, testing=args.test, dataset_root=args.dataset_root)
    if args.test and root.resolve() == get_dataset_root(config).resolve():
        parser.error("--test must not use the production dataset root")
    collect(config, root)


if __name__ == "__main__":
    main()
