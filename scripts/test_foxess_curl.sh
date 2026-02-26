#!/usr/bin/env bash
# Test Fox ESS Open API real-time query with curl.
# Loads FOXESS_API_KEY and device SN from the project .env automatically.
#
# Usage (from project root):
#   ./scripts/test_foxess_curl.sh
#
# .env should have FOXESS_API_KEY (or FOX_API_KEY) and either FOXESS_DEVICE_SN
# or INVERTER_SERIAL_NUMBER.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from project root if present
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

PATH_REQ="/op/v0/device/real/query"
# Trim whitespace and carriage return (CRLF in .env can break the signature)
API_KEY=$(printf '%s' "${FOX_API_KEY:-$FOXESS_API_KEY}" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SN=$(printf '%s' "${FOXESS_DEVICE_SN:-$INVERTER_SERIAL_NUMBER}" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

if [ -z "$API_KEY" ] || [ -z "$SN" ]; then
  echo "Missing config. In $ROOT/.env set:"
  echo "  FOXESS_API_KEY (or FOX_API_KEY)"
  echo "  FOXESS_DEVICE_SN or INVERTER_SERIAL_NUMBER"
  exit 1
fi

# Timestamp in milliseconds (required by API)
TIMESTAMP=$(python3 -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo $(($(date +%s) * 1000)))

# Signature = MD5(path + "\\r\\n" + token + "\\r\\n" + timestamp) per Fox ESS Open API doc.
# The doc's Python example uses fr'...\r\n...' which in a *raw* string is literal backslash-r-backslash-n (4 chars), not CRLF.
SIGNATURE=$(python3 -c "
import hashlib, sys
path = sys.argv[1].strip()
token = sys.argv[2].strip()
ts = sys.argv[3].strip()
s = path + r'\r\n' + token + r'\r\n' + ts
print(hashlib.md5(s.encode()).hexdigest())
" "$PATH_REQ" "$API_KEY" "$TIMESTAMP")

echo "--- Request (path, timestamp, signature built) ---"
echo "  path: $PATH_REQ"
echo "  timestamp: $TIMESTAMP"
echo "  signature: ${SIGNATURE:0:8}..."
echo ""
echo "--- Response ---"

curl -s -X POST "https://www.foxesscloud.com${PATH_REQ}" \
  -H "token: $API_KEY" \
  -H "timestamp: $TIMESTAMP" \
  -H "signature: $SIGNATURE" \
  -H "lang: en" \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (compatible; FoxESS-Test/1.0)" \
  -d "{\"sn\":\"$SN\",\"variables\":[\"SoC\",\"pvPower\",\"gridConsumptionPower\",\"feedinPower\",\"batChargePower\",\"batDischargePower\",\"loadsPower\",\"generationPower\",\"workMode\"]}" \
  | python3 -m json.tool

echo ""
echo "Done. Check errno (0 = success); result is the raw API payload (list of devices with datas or single device)."
