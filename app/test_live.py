import urllib.request
import urllib.error
import json

url = "https://the-data-wrangler.onrender.com/api/v1/ingest"
headers = {
    "X-Wrangler-Token": "WranglerSecureToken2026_NC",
    "Content-Type": "text/csv"
}
# Fresh ID (104) to avoid duplicate key errors
data = "9305305350104,Don,Afrikha,12,85.5,0.98".encode('utf-8')

print("Sending live request to Render...")
req = urllib.request.Request(url, data=data, headers=headers, method='POST')

try:
    with urllib.request.urlopen(req) as response:
        print(f"HTTP Status: {response.status}")
        response_data = response.read().decode('utf-8')
        print("Response:", response_data)
except urllib.error.HTTPError as e:
    print(f"FAILED with HTTP Error: {e.code}")
    print(e.read().decode('utf-8'))
except Exception as e:
    print(f"FAILED: {e}")