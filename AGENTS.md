# Green Path coding standard

Write direct, readable research code. Apply these rules to future changes in this
project unless the user explicitly requests a different approach.

- Follow a simple pipeline: read inputs, identify pending records, fetch sources,
  process data, merge and save.
- Before retaining an operation, ask whether it is needed for correct collection,
  processing, saving or simple incremental execution. Delete redundant work.
- Prefer plain functions and existing pandas, GeoPandas and Rasterio operations.
  Do not introduce classes, managers, storage abstractions or orchestration layers
  unless the task requires them.
- Extract meaningful processing steps; do not create helpers for trivial checks
  or merely relocate complicated code. Do not impose arbitrary line limits.
- Preserve algorithms, identifiers, Parquet schemas, relative paths, existing
  Tile Set identities and test/production separation unless explicitly changed.
- Reuse completed outputs. Avoid checksum rereads, verification-only HTTP calls,
  dependency audits and repeated source parsing. Retain only checks needed for
  correct results, recoverable download failures or safe dataset selection.
- Catch errors only when processing can recover and continue. Let unexpected
  programming, configuration and storage errors fail normally.
- Keep source identification and metadata minimal. Preserve legacy columns when
  required by output contracts; leave dataset versioning to DVC and Git tags.
  Those tools do not recreate mutable external sources automatically.
- Configure real processing choices, not routine implementation details. Keep
  compatibility inputs only when needed to preserve existing data identities.
- Use concise names and comments explaining non-obvious decisions. Avoid lengthy
  docstrings, redundant comments, precautionary branches and unnecessary logging.
- Verify changes with syntax/import checks and focused regressions. Use isolated
  fixtures or existing small test data; never modify production during testing.
  Report exactly what was tested and any deliberate integrity-check tradeoffs.
