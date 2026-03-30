#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
cd frontend
npm run build
echo "Frontend built to frontend/dist/"
