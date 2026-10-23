# Green Path Data Versioning

Use DVC to retain dataset bytes in GCS and Git to retain the corresponding data
pointers, code and configuration. GitHub is optional for local versioning.

## Start the existing environment

From the repository root in Linux/WSL:

```bash
cd data-versioning
bash docker-shell.sh
```

Check the bucket, project and secrets directory in `docker-shell.sh` first. The
existing entrypoint authenticates using `/secrets/data-service-account.json`,
mounts the bucket at `/mnt/gcs_data`, and activates the DVC environment. It also
exposes the working `datasets/` directory at `/app/greenpath_dataset` without copying it.

Inside the container, run the examples from the repository root:

```bash
cd /app
```

DVC commands can run elsewhere within the same Git/DVC repository, but relative
paths in this guide assume its root. Do not run `dvc init` again or create another
remote. The default remote in `.dvc/config` is already `greenpath_dataset`.

## Git and DVC responsibilities

| Command | What it saves |
| --- | --- |
| `git commit` | Staged code, configuration and `.dvc` metadata locally |
| `git push` | Git commits to GitHub; tags must also be explicitly pushed |
| `dvc add <local-path>` | Tracking metadata and cached content for local data; no remote upload |
| `dvc push` | DVC-tracked cached data to the configured GCS remote |
| `dvc import-url --to-remote` | An external dataset directly into the DVC remote, plus a local `.dvc` pointer |

Git tags identify commits containing code, configuration and DVC pointers. Tags
do not store images or Parquet files. `git push` and `dvc push` are independent;
neither substitutes for the other. A Git commit alone does not snapshot mutable
data under `gs://.../datasets/`.

## GCS layout

```text
gs://ac215-final-project-test2/
+-- datasets/
|   +-- _test/
|   |   +-- boston/
|   +-- boston/
|   +-- new_york/
+-- dvc_store/
```

`datasets/` contains working datasets written by Data Processor.
`datasets/_test/boston/` is test storage; `datasets/boston/` and other city
directories hold production data as those cities are collected. The default city
and processing paths come from [`configs/data.yaml`](../configs/data.yaml).

`dvc_store/` is the separate, content-addressed DVC remote holding retained
versions. The configured path uses an underscore (`dvc_store`), even though the
proposed layout in [`docs/data_structure.md`](../docs/data_structure.md) also
mentions `dvc-store`. Use the existing configuration; do not move or delete objects.

## Snapshot the Boston test dataset

Finish collection before snapshotting, and keep the source unchanged during the
import so the files form one consistent state. From `/app`:

```bash
mkdir -p training_data
dvc import-url --to-remote \
  gs://ac215-final-project-test2/datasets/_test/boston \
  training_data/boston_test

git add training_data/boston_test.dvc
git commit -m "Snapshot Boston test dataset"
git tag -a data-boston-test-v1 -m "Boston test dataset snapshot"
```

The import creates `training_data/boston_test.dvc` and stores the imported content
in the configured DVC remote without first downloading the complete dataset into
the local workspace. This operation already uploads the snapshot; a separate
`dvc push` is not needed for this import. It does not modify the source dataset.
If DVC creates or updates `training_data/.gitignore`, stage that file too before
committing. Commit the relevant code/configuration changes with the pointer when
they are part of the snapshot's processing definition.

No `git push` is required for the local commit, tag, or data retained in GCS.
Sharing or backing up those Git references to GitHub is a separate operation.

When the working source changes, update the same import:

```bash
dvc update --to-remote training_data/boston_test.dvc
git add training_data/boston_test.dvc
git commit -m "Update Boston test dataset snapshot"
```

The new commit retains the updated pointer; the earlier commit/tag retains the
previous pointer. Create snapshots **before** source data is overwritten if its
previous state must be recoverable. DVC cannot recover an earlier state that was
never imported and retained.

## Restore a snapshot

To restore the data referenced by the currently checked-out pointer:

```bash
dvc pull training_data/boston_test.dvc
```

This materializes the dataset under the local `training_data/boston_test/`
directory. It does not restore over the working GCS `datasets/_test/boston/` source.

To inspect or recover the tagged version, first commit or set aside unrelated
working changes, then run:

```bash
git switch --detach data-boston-test-v1
dvc pull training_data/boston_test.dvc
```

The tag selects the old `.dvc` metadata; DVC retrieves the actual bytes from GCS.
Restoration requires the referenced objects to remain available in the remote.
Detached HEAD is for inspection/recovery. Return to your development branch before
continuing ordinary work, and pull its pointer again if you need its data locally.

## Multiple cities

Keep independent pointers for each city and the test dataset:

```text
training_data/
+-- boston.dvc
+-- new_york.dvc
+-- boston_test.dvc
```

This is the proposed organization, not an implemented training pipeline. A future
`configs/train.yaml` can select cities and a `tile_set_id`; the `.dvc` files and
Git commit determine the dataset versions. Selecting a Tile Set alone does not
identify all versions of its accompanying indexes and feature tables.

## Usage notes

- Use the existing Data Versioning container for DVC dependencies and GCP credentials.
- Do not manually edit or remove objects inside `dvc_store/`.
- Do not commit raw datasets or secrets to Git; commit pointers and relevant configuration.
- Retain snapshot objects in the remote before relying on them for reproducibility.
- Command syntax was checked against DVC 3.67.1 using `--version` and `--help`.
  No import, update, push, pull or dataset modification was performed to write this guide.
