#!/bin/bash
set -e

# read mapillary token from secrets file
export MAPILLARY_TOKEN="$(cat /secrets/mapillary_token.txt)"

gcloud auth activate-service-account --key-file=$GOOGLE_APPLICATION_CREDENTIALS
mkdir -p /mnt/gcs_data
gcsfuse \
  --key-file="$GOOGLE_APPLICATION_CREDENTIALS" \
  "$GCS_BUCKET_NAME" \
  /mnt/gcs_data

source /.venv/bin/activate

exec /bin/bash