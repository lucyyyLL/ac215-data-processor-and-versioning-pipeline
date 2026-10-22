"""Shared YAML loading and dataset paths for the GCSFuse mount."""

from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs/data.yaml"
MOUNT = Path("/mnt/gcs_data")


def load_config(config_path=CONFIG_PATH):
    with Path(config_path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def get_dataset_root(config, testing=False, dataset_root=None):
    if dataset_root is not None:
        return Path(dataset_root)
    root = MOUNT / config["storage"]["datasets_prefix"]
    if testing:
        root = root / "_test"
    return root / config["city"]["name"]
