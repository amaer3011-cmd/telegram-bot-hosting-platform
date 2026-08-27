#!/bin/sh
set -eu

mkdir -p /app/data /app/data/uploaded_bots /app/logs
chown -R appuser:appuser /app/data /app/logs

exec gosu appuser "$@"
