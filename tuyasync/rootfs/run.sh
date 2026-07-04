#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Pull add-on options into the environment for the Python app.
export TUYA_API_KEY="$(bashio::config 'api_key')"
export TUYA_API_SECRET="$(bashio::config 'api_secret')"
export TUYA_API_REGION="$(bashio::config 'api_region')"
export TUYA_API_DEVICE_ID="$(bashio::config 'api_device_id')"
export TUYA_SCAN_RETRIES="$(bashio::config 'scan_retries')"

# Supervisor provides this automatically; the app uses it to reach the
# Home Assistant Core API for reading/updating Tuya Local config entries.
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

# Working dir for tinytuya's snapshot.json / devices.json output
export TUYA_WORKDIR="/share/tuyasync"
mkdir -p "${TUYA_WORKDIR}"

bashio::log.info "Starting TuyaSync on :8099 (host_network) ..."
cd /app
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8099 --no-access-log
