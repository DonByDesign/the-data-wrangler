# The Data Wrangler: SA-SAMS Data Cleaning Engine

[![Security Audit](https://github.com/DonByDesign/the-data-wrangler/actions/workflows/security-audit.yml/badge.svg)](https://github.com/DonByDesign/the-data-wrangler/actions/workflows/security-audit.yml)

A zero-dependency, high-performance data ingestion engine designed to validate, sanitize, and sync South African school data (SA-SAMS). Built with a DevSecOps-first architecture, this project demonstrates how to achieve enterprise-grade security and cloud scalability without relying on third-party frameworks.

## The Engineering Pivot (v1 → v2)

**The Initial Build (v1):** The prototype utilized FastAPI and Pydantic for rapid development. While effective, the combined RAM footprint of the IDE and the framework's dependencies caused severe system lag on resource-constrained hardware.

**The Refactor (v2):** To eliminate the resource bottleneck and drastically shrink the supply-chain attack surface, the architecture was entirely refactored to a **zero-dependency native Python** approach (`ThreadingHTTPServer`, `re`, `json`, `urllib`). 

**The Result:**
* **Zero Dependencies:** No `requirements.txt`. No CVEs from third-party packages.
* **Minimal Memory Footprint:** Runs flawlessly on low-spec hardware and scales down to zero on Google Cloud Run.
* **Pure Stdlib Validation:** All Pydantic models were replaced with standard Python dictionaries and regex validation, preserving all security controls.

## Security & DevSecOps Features

* **Input Sanitization:** Custom regex rules block SQL injection, XSS, and HTML payloads.
* **SA ID Validation:** Strict 13-digit South African ID validation.
* **Database Security:** Supabase PostgreSQL utilizing Row Level Security (RLS) to ensure data isolation.
* **Automated CI/CD Pipeline:** Every push triggers a GitHub Actions workflow that runs:
  * **Gitleaks:** Scans for accidentally committed API keys or secrets.
  * **CodeQL:** GitHub's advanced static analysis engine for Python.
  * **Zero-Dependency Guard:** A custom AST (Abstract Syntax Tree) script that fails the build if *any* third-party package is imported, enforcing the architectural contract.

## Tech Stack & Architecture

* **Backend:** Python 3.11+ (Standard Library only: `http.server`, `urllib`, `json`, `re`)
* **Database:** Supabase (PostgreSQL + REST API)
* **Cloud Infrastructure:** Google Cloud Run (Serverless, Scale-to-Zero)
* **CI/CD:** GitHub Actions

## Business Context
In South Africa, schools struggle with messy SA-SAMS data exports. *The Data Wrangler* provides a secure, lightweight API to ingest, clean, and centralize this data. The SaaS model targets at R2,000/school/month, with a highly defensible >99.5% gross margin due to the serverless, zero-dependency architecture.

## Local Development

Because this project is entirely zero-dependency, running it locally takes seconds:

```bash
# 1. Clone the repo
git clone https://github.com/DonByDesign/the-data-wrangler.git

# 2. Set environment variables (Supabase URL & Key)
$env:SUPABASE_URL="your-url"
$env:SUPABASE_KEY="your-key"

# 3. Run the engine
python app/main.py
