#!/bin/bash
set -e

gcloud auth activate-service-account --key-file=$GOOGLE_APPLICATION_CREDENTIALS
mkdir -p /mnt/gcs_data
gcsfuse \
  --key-file="$GOOGLE_APPLICATION_CREDENTIALS" \
  "$GCS_BUCKET_NAME" \
  /mnt/gcs_data

source /.venv/bin/activate

exec /bin/bash