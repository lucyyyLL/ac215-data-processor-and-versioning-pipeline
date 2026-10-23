"""Run Streetview, NAIP and OSM/WorldCover collection in one dataset."""

import argparse
import os
from pathlib import Path

from src import naip, osm_features, streetview
from src.config import CONFIG_PATH, get_dataset_root, load_config


def run_pipeline(config, root, steps, bbox=None, sample_size=None):
    if "streetview" in steps:
        bbox = bbox if bbox is not None else config["city"]["bbox"]
        sample_size = sample_size if sample_size is not None else config["mapillary"]["sampling"]["sample_size"]
        streetview.collect(config, root, bbox, sample_size, os.environ["MAPILLARY_TOKEN"])
    if "naip" in steps:
        naip.collect(config, root)
    if "osm" in steps:
        osm_features.collect(config, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Use datasets/_test/<city>")
    parser.add_argument("--steps", nargs="+", choices=["streetview", "naip", "osm"],
                        default=["streetview", "naip", "osm"])
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--sample-size", type=int, help="Streetview only; 0 uses all eligible cells")
    args = parser.parse_args()
    config = load_config(args.config)
    overrides = "streetview" in args.steps and (args.bbox is not None or args.sample_size is not None)
    testing = args.test or overrides
    root = get_dataset_root(config, testing=testing, dataset_root=args.dataset_root)
    if testing and root.resolve() == get_dataset_root(config).resolve():
        parser.error("Test runs and Streetview overrides must use a different dataset root")
    if "streetview" in args.steps:
        if config["spatial"]["h3_resolution"] != 10 or config["mapillary"]["metadata"]["tile_zoom"] != 14:
            parser.error("Green Path requires H3 R10 and Mapillary zoom 14")
        if args.sample_size is not None and args.sample_size < 0:
            parser.error("--sample-size must be nonnegative")
        bbox = args.bbox if args.bbox is not None else config["city"]["bbox"]
        if not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
            parser.error("--bbox must be WEST SOUTH EAST NORTH")
    run_pipeline(config, root, args.steps, args.bbox, args.sample_size)


if __name__ == "__main__":
    main()
