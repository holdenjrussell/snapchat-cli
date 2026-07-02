#!/usr/bin/env bash
# Snapchat Ads session context loader
# Runs on every Claude Code session start
# Outputs account health to stdout (becomes conversation context)

# Resolve the cli/ dir relative to this script (hooks/ lives inside it)
DIR="$(cd "$(dirname "$0")/.." && pwd)"
SNAP="uv run --directory $DIR snapchat-ads"

# Bail silently if the CLI is not installed yet
$SNAP --help >/dev/null 2>&1 || exit 0

echo "# Snapchat Ads Account Status"
echo ""

for acct in default; do
  status_json=$($SNAP --account "$acct" auth status 2>/dev/null)
  status=$(printf '%s' "$status_json" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  print(d.get('auth_status','UNKNOWN'))
except: print('ERROR')
" 2>/dev/null)

  expires_in=$(printf '%s' "$status_json" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  print(d.get('expires_in_human','-'))
except: print('-')
" 2>/dev/null)

  if [ "$status" = "NO_TOKEN" ] || [ "$status" = "ERROR" ]; then
    echo "- **$acct**: TOKEN MISSING -- run \`snapchat-ads --account $acct auth login\`"
  elif [ "$status" = "EXPIRED_REFRESH_AVAILABLE" ]; then
    echo "- **$acct**: token EXPIRED, refresh available -- run \`snapchat-ads --account $acct auth refresh\`"
  elif [ "$status" = "EXPIRING_SOON" ]; then
    echo "- **$acct**: token EXPIRING SOON ($expires_in) -- consider \`auth refresh\`"
  else
    echo "- **$acct**: token $status (expires in $expires_in)"
  fi
done

echo ""
echo "Use \`snapchat-ads --account <name> --human <command>\` or ask naturally."
