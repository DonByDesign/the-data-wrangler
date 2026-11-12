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

## Local Development

Because this project is entirely zero-dependency, running it locally takes seconds:

```bash
# 1. Clone the repo
git clone https://github.com/DonByDesign/the-data-wrangler.git

# 2. Set environment variables (Supabase URL & Key)
$env:SUPABASE_URL="your-url"
$env:SUPABASE_KEY="your-key"

## The Real-World Problem
In South Africa, schools use SA-SAMS (School Administration & Management System) to manage learner data. However, before this data can be uploaded to the Department of Basic Education's **DDD (Data Driven Districts)** dashboard, it must be manually validated using a desktop tool called Valistractor. 

This process is manual, highly error-prone, and purely focused on compliance — it offers zero analytics or intervention strategies for the school itself. Furthermore, third-party Python dependencies (like Pydantic) often fail to compile on low-resource edge devices (like Android/Termux) due to missing Rust/C++ build tools.

## The Solution
**The Data Wrangler** is a headless, zero-dependency API that automates the SA-SAMS extraction, validation, and sanitization process. 
1. **Automated Validation:** Replaces manual Valistractor red-flag hunting with automated regex validation (13-digit IDs, missing fields).
2. **Security First:** Strips XSS and SQL injection payloads before data ever touches a database.
3. **Actionable Analytics:** Syncs clean data to Supabase, enabling Looker Studio dashboards for principals to monitor attendance anomalies and academic interventions in real-time.
4. **Edge-Ready:** By relying entirely on the Python Standard Library, the engine runs flawlessly on Cloud Run, edge servers, or Android devices (Termux) without compilation errors.

# 3. Run the engine
python app/main.py
