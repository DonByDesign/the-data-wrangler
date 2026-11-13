except urllib.error.HTTPError as e:
    print(f"FAILED with HTTP Error: {e.code}")
    print(e.read().decode('utf-8'))
except Exception as e:
    print(f"FAILED: {e}")