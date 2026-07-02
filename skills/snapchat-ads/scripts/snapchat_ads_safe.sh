#!/usr/bin/env bash
# Safe wrapper around the snapchat-ads CLI for agent invocation.
# Forces --human output, scopes to a single account, and fails closed on
# missing env vars rather than letting the CLI guess.

set -euo pipefail

if [ "${SNAPCHAT_ADS_SAFE:-0}" != "1" ]; then
  echo "ERROR: snapchat_ads_safe.sh refuses to run without SNAPCHAT_ADS_SAFE=1" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$REPO_ROOT/cli"
if [ ! -d "$CLI_DIR" ]; then
  echo "ERROR: snapchat-ads CLI not installed at $CLI_DIR" >&2
  exit 2
fi

ACCOUNT="${SNAPCHAT_ADS_ACCOUNT:-default}"

if [ "$#" -lt 1 ]; then
  echo "Usage: SNAPCHAT_ADS_SAFE=1 $0 <group> [args...]" >&2
  exit 2
fi

exec uv run --directory "$CLI_DIR" snapchat-ads --account "$ACCOUNT" --human "$@"
