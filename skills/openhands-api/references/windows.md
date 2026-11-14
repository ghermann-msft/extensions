# Windows Notes for OpenHands API Commands

Use these PowerShell forms when running this skill's shell examples natively on Windows.

## Environment Variables

```powershell
$env:OPENHANDS_CLOUD_API_URL = "https://your-openhands-app"
$env:OPENHANDS_CLOUD_API_KEY = "..."
```

When `OPENHANDS_CLOUD_API_URL` and `OPENHANDS_API_KEY` are both present in a hosted sandbox, use that pair instead. Do not print either key.

## Curl Examples

Call `curl.exe` and use PowerShell backticks for line continuation:

```powershell
$baseUrl = if ($env:OPENHANDS_CLOUD_API_URL) { $env:OPENHANDS_CLOUD_API_URL.TrimEnd("/") } else { "https://app.all-hands.dev" }
$apiKey = if ($env:OPENHANDS_CLOUD_API_URL -and $env:OPENHANDS_API_KEY) { $env:OPENHANDS_API_KEY } elseif ($env:OPENHANDS_CLOUD_API_KEY) { $env:OPENHANDS_CLOUD_API_KEY } else { $env:OPENHANDS_API_KEY }

curl.exe -X POST "$baseUrl/api/v1/app-conversations" `
  -H "Authorization: Bearer $apiKey" `
  -H "Content-Type: application/json" `
  -d '{
    "generate_title_on_submit": true,
    "initial_message": {
      "role": "user",
      "content": [{"type": "text", "text": "Investigate flaky tests in tests/test_api.py."}]
    },
    "selected_repository": "owner/repo"
  }'
```

## Python Here-doc Debugging Examples

PowerShell does not support Bash here-docs like `python3 - <<'PY'`. Save the script to a temp file and pipe the API response into it.

```powershell
$script = Join-Path $env:TEMP "openhands-events.py"
@'
import json, sys
items = (json.load(sys.stdin) or {}).get("items", [])
for i, e in enumerate(items):
    print(f"{i:04d}  {e.get('timestamp','')}  {e.get('source','')}  {e.get('kind','')}")
'@ | Set-Content -LiteralPath $script -Encoding utf8

curl.exe -s "$baseUrl/api/v1/conversation/$env:APP_CONVERSATION_ID/events/search?limit=100" `
  -H "Authorization: Bearer $apiKey" `
  -H "Accept: application/json" |
  python $script
```
