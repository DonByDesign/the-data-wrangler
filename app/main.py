import os
import re
import json
import io
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
from datetime import datetime

# 1. Target Endpoint Config (Clean the strings to prevent DNS errors)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("CRITICAL: Missing environment variables. Server refusing to start.")