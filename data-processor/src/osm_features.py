"""Extract OSM and WorldCover features: python -m src.osm_features --test."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd
import planetary_computer
import pyrosm
import pystac_client
import rasterio
from rasterio.windows import Window
import requests
from shapely.geometry import Point, shape

from src.config import CONFIG_PATH, get_dataset_root, load_config
from src.storage import save_parquet


def get_osm_snapshot(config, root, previous):
    settings, outputs = config["features"]["osm"], config["outputs"]
    selected = settings["snapshot_path"]
    if not selected and previous.get("source_url") == settings["pbf_url"]:
        selected = previous.get("path")
    path = root / (selected or "")
    if path.is_file():
        return path

    with tempfile.TemporaryDirectory(prefix="greenpath-osm-download-") as directory:
        local = Path(directory) / "source.osm.pbf"
        digest = hashlib.sha256()
        with requests.get(settings["pbf_url"], stream=True, timeout=60) as response:
            response.raise_for_status()
            with local.open("wb") as stream:
                for chunk in response.iter_content(1 << 20):
                    stream.write(chunk)
                    digest.update(chunk)
        sha256 = digest.hexdigest()
        fields = dict(osm_region=settings["region"], osm_snapshot_sha256=sha256)
        path = root / outputs["osm_pbf"].format(**fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            shutil.copyfile(local, path)
        sidecar = root / outputs["osm_download_metadata"].format(**fields)
        if not sidecar.exists():
            sidecar.write_text(json.dumps(dict(source_url=settings["pbf_url"], sha256=sha256,
                                               downloaded_at=datetime.now(timezone.utc).isoformat()), indent=2))
    return path


def load_osm(pbf, bbox, crs, settings):
    # Pyrosm parses a temporary local file; the original snapshot stays in GCS.
    with tempfile.TemporaryDirectory(prefix="greenpath-osm-") as directory:
        local = Path(directory) / "source.osm.pbf"
        shutil.copyfile(pbf, local)
        osm = pyrosm.OSM(str(local), bounding_box=list(bbox))
        nodes, edges = osm.get_network(network_type=settings["network_type"], nodes=True,
                                       extra_attributes=["lanes", "maxspeed", *settings["bike_tags"]])
        if edges is None:
            edges = gpd.GeoDataFrame(columns=["u", "v", "highway", "lanes", "maxspeed"], geometry=[], crs=4326)
        edges = edges.to_crs(crs).reset_index(drop=True)
        if nodes is None:
            nodes = gpd.GeoDataFrame(columns=["id"], geometry=[], crs=4326)
        nodes = nodes.to_crs(crs)
        degree = pd.concat([edges.u, edges.v]).value_counts()
        nodes["street_count"] = nodes.id.map(degree).fillna(0).astype(int)

        def layer(tags):
            data = osm.get_data_by_custom_criteria(custom_filter=tags, filter_type="keep",
                                                  keep_nodes=False, keep_ways=True, keep_relations=True)
            if data is None or data.empty:
                return gpd.GeoDataFrame(geometry=[], crs=crs)
            return data[["geometry"]].reset_index(drop=True).to_crs(crs)

        data = {name: layer(tags) for name, tags in settings["polygon_filters"].items()}
        pois = osm.get_pois(custom_filter={tag: True for tag in settings["poi_tags"]})
        if pois is None or pois.empty:
            pois = gpd.GeoDataFrame(geometry=[], crs=crs)
        else:
            pois = pois[["geometry"]].to_crs(crs).reset_index(drop=True)
            pois["geometry"] = pois.geometry.representative_point()
    return dict(edges=edges, nodes=nodes, pois=pois, **data)


def first(value):
    return (value[0] if value else None) if isinstance(value, (list, tuple)) else value


def speed_kmh(value):
    if isinstance(value, (list, tuple)) and len(value) != 1:
        return np.nan
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?", str(first(value)).strip(), re.IGNORECASE)
    if not match:
        return np.nan
    return float(match[1]) * (1.609344 if (match[2] or "").lower() == "mph" else 1)


def extract_osm_features(foot, data, settings):
    edges, nodes, pois = data["edges"].copy(), data["nodes"], data["pois"]
    classes = settings["road_classes"]
    edges["rclass"] = edges.highway.map(lambda h: next((k for k, tags in classes.items() if first(h) in tags), "other"))

    def has_bike(row):
        if first(row.get("highway")) == "cycleway":
            return False  # Separately mapped paths remain in road_path_m.
        return any(isinstance(first(row.get(tag)), str) and first(row.get(tag)) in settings["bike_accepted_values"]
                   for tag in settings["bike_tags"])

    def to_num(value):
        try:
            return float(str(first(value)).split()[0])
        except (ValueError, TypeError, IndexError):
            return np.nan

    edges["bike"] = edges.apply(has_bike, axis=1) if not edges.empty else pd.Series(dtype=bool)
    edges["lanes_n"] = edges.lanes.map(to_num) if "lanes" in edges else np.nan
    edges["speed"] = edges.maxspeed.map(speed_kmh) if "maxspeed" in edges else np.nan
    intersections = nodes[nodes.street_count >= settings["intersection_min_degree"]]

    def area_frac(layer, fp):
        if layer.empty:
            return 0.0
        candidates = layer.iloc[layer.sindex.query(fp, predicate="intersects")]
        return float(min(candidates.geometry.intersection(fp).union_all().area / fp.area, 1.0)) if not candidates.empty else 0.0

    def wmean(values, weights):
        valid = values.notna()
        return float(np.average(values[valid], weights=weights[valid])) if valid.any() and weights[valid].sum() > 0 else np.nan

    rows = []
    for cell in foot.itertuples():
        fp = cell.geometry
        roads = edges.iloc[edges.sindex.query(fp, predicate="intersects")].copy()
        roads["len_in"] = roads.geometry.intersection(fp).length
        row = {"h3_index": cell.h3_index}
        for category in [*classes, "other"]:
            row[f"road_{category}_m"] = float(roads.loc[roads.rclass == category, "len_in"].sum())
        row.update(road_total_m=float(roads.len_in.sum()), bike_lane_m=float(roads.loc[roads.bike, "len_in"].sum()))
        drive = roads[roads.rclass.isin(["major", "secondary", "local"])]
        row.update(lanes_avg=wmean(drive.lanes_n, drive.len_in), maxspeed_avg=wmean(drive.speed, drive.len_in),
                   intersections=int(len(intersections.iloc[intersections.sindex.query(fp, predicate="intersects")])),
                   poi_count=int(len(pois.iloc[pois.sindex.query(fp, predicate="intersects")])) if len(pois) else 0)
        for name, layer_name in [("water", "water"), ("green", "green"), ("building", "buildings")]:
            row[f"osm_{name}_frac"] = area_frac(data[layer_name], fp)
        rows.append(row)
    return pd.DataFrame(rows)


def load_worldcover(cells, bbox, settings):
    catalog = pystac_client.Client.open(settings["stac_api"], modifier=planetary_computer.sign_inplace)
    year = settings["year"]
    items = sorted(catalog.search(collections=[settings["collection"]], bbox=list(bbox),
                                  datetime=f"{year}-01-01/{year}-12-31").items(), key=lambda item: item.id)
    items = [item for item in items if settings["product_version"] in item.id.split("_")]
    geometries = [(shape(item.geometry), item) for item in items]
    assignments = {cell.cell_id: next((item for geometry, item in geometries if geometry.contains(Point(cell.lon, cell.lat))), None)
                   for cell in cells.itertuples()}
    return assignments


def worldcover_fractions(array, settings):
    classes = settings["classes"]
    valid = np.asarray(array)[np.asarray(array) != settings["nodata_value"]]
    row = {f"wc_{name}_frac": np.nan for name in classes.values()}
    row["wc_dominant"] = None
    if valid.size:
        counts = np.bincount(valid.ravel(), minlength=max(classes) + 1)
        row.update({f"wc_{name}_frac": float(counts[code] / valid.size) for code, name in classes.items()})
        row["wc_dominant"] = classes.get(int(counts.argmax()))
    return row


def extract_worldcover_features(cells, foot, assignments, settings):
    foot_ll = foot.to_crs(4326)
    groups, rows = {}, []
    for i, cell in enumerate(cells.itertuples()):
        item = assignments[cell.cell_id]
        row = dict(h3_index=cell.h3_index, **worldcover_fractions(np.array([], dtype=np.uint8), settings))
        rows.append(row)
        if item is not None:
            groups.setdefault(item.id, (item, []))[1].append((i, row))
    for item, group in groups.values():
        href = planetary_computer.sign(item.assets[settings["asset"]].href)
        with rasterio.open(href) as src:
            for i, row in group:
                minx, miny, maxx, maxy = foot_ll.geometry.iloc[i].bounds
                r0, c0 = src.index(minx, maxy)
                r1, c1 = src.index(maxx, miny)
                array = src.read(1, window=Window(c0, r0, max(c1 - c0 + 1, 1), max(r1 - r0 + 1, 1)))
                row.update(worldcover_fractions(array, settings))
    return pd.DataFrame(rows)


def collect(config, root):
    root = Path(root).resolve()
    settings, outputs = config["features"], config["outputs"]
    cells = pd.read_parquet(root / outputs["cells"]).reset_index(drop=True)
    output = root / outputs["osm_features"]
    metadata_path = output.with_suffix(".meta.json")
    previous_text = metadata_path.read_text() if metadata_path.exists() else ""
    previous = json.loads(previous_text) if previous_text else {}
    if cells.empty:
        return pd.read_parquet(output) if output.exists() else pd.DataFrame()

    points = gpd.GeoSeries(gpd.points_from_xy(cells.lon, cells.lat), crs=4326)
    crs = points.estimate_utm_crs()
    west, south, east, north = points.total_bounds
    pad = settings["osm"]["bbox_pad_degrees"]
    bbox = (west - pad, south - pad, east + pad, north + pad)
    pbf = get_osm_snapshot(config, root, previous.get("osm_snapshot", {}))
    metadata = json.loads(json.dumps(dict(features=settings, metric_crs=str(crs),
                                         osm_snapshot=dict(path=str(pbf), source_url=settings["osm"]["pbf_url"]))))
    source_items = previous.pop("worldcover_items", [])
    compatible = previous == metadata
    old = pd.read_parquet(output) if output.exists() and compatible else pd.DataFrame(columns=["cell_id"])
    pending_mask = ~cells.cell_id.isin(old.cell_id)
    pending = cells.loc[pending_mask].reset_index(drop=True)
    if pending.empty:
        print(f"{root}: all {len(cells)} feature rows reused")
        return old

    pending_points = points.loc[pending_mask].reset_index(drop=True)
    selected_foot = gpd.GeoDataFrame({"h3_index": pending.h3_index},
                                    geometry=pending_points.to_crs(crs).buffer(settings["ground_m"] / 2, cap_style="square"), crs=crs)
    assignments = load_worldcover(pending, bbox, settings["worldcover"])
    data = load_osm(pbf, bbox, crs, settings["osm"])
    osm = extract_osm_features(selected_foot, data, settings["osm"])
    wc = extract_worldcover_features(pending, selected_foot, assignments, settings["worldcover"])
    fresh = pending[["cell_id", "h3_index"]].merge(osm, on="h3_index").merge(wc, on="h3_index")
    fresh["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    combined = fresh if old.empty else pd.concat([old, fresh], ignore_index=True)
    combined = combined.drop_duplicates("cell_id", keep="last").sort_values("cell_id").reset_index(drop=True)
    save_parquet(combined, output)
    metadata["worldcover_items"] = sorted(set(source_items if compatible else []) |
                                        {item.id for item in assignments.values() if item is not None})
    metadata_path.write_text(json.dumps(metadata, indent=2))
    print(f"{root}: {len(fresh)} cells computed, {len(combined)} total feature rows")
    return combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--dataset-root", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    root = get_dataset_root(config, testing=args.test, dataset_root=args.dataset_root)
    if args.test and root.resolve() == get_dataset_root(config).resolve():
        parser.error("--test must not use the production dataset root")
    collect(config, root)


if __name__ == "__main__":
    main()
