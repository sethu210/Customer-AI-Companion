"""
Apply a SQL file to Databricks through the SQL Statement Execution API, one statement at a time.

Auth comes from the Databricks CLI login (`databricks auth login`); the token is never printed.
Statements in the file are separated by a line containing only ";". "{catalog}" is substituted.
The workspace and warehouse come from DATABRICKS_HOST and DATABRICKS_WAREHOUSE_ID (or --host / --warehouse-id).

Usage (from the project root):
    python -m test_data.databricks.run_sql --file test_data/databricks/uc_functions.sql [--catalog workspace]
"""

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_HOST = os.environ.get("DATABRICKS_HOST")
DEFAULT_WAREHOUSE = os.environ.get("DATABRICKS_WAREHOUSE_ID")


def cli_token(cli: str, host: str) -> str:
    out = subprocess.run([cli, "auth", "token", "--host", host, "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["access_token"]


def call(host: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        host + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def execute(host: str, token: str, warehouse: str, statement: str) -> dict:
    res = call(host, token, "POST", "/api/2.0/sql/statements",
               {"warehouse_id": warehouse, "statement": statement, "wait_timeout": "50s"})
    while res["status"]["state"] in ("PENDING", "RUNNING"):
        time.sleep(3)
        res = call(host, token, "GET", f"/api/2.0/sql/statements/{res['statement_id']}")
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE)
    parser.add_argument("--cli", default=shutil.which("databricks") or "databricks")
    args = parser.parse_args()
    if not args.host or not args.warehouse_id:
        parser.error("set DATABRICKS_HOST and DATABRICKS_WAREHOUSE_ID (or pass --host and --warehouse-id)")

    text =Path(args.file).read_text(encoding="utf-8-sig").replace("{catalog}", args.catalog)
    chunks = [c.strip() for c in text.split("\n;\n")]
    statements = [c for c in chunks if any(not line.strip().startswith("--") and line.strip() for line in c.splitlines())]
    token = cli_token(args.cli, args.host)

    failed = 0
    for n, stmt in enumerate(statements, start=1):
        first = next(line for line in stmt.splitlines() if line.strip() and not line.strip().startswith("--"))
        started = time.monotonic()
        res = execute(args.host, token, args.warehouse_id, stmt)
        state = res["status"]["state"]
        rows = res.get("manifest", {}).get("total_row_count", "-")
        print(f"[{n}/{len(statements)}] {state:<9} {time.monotonic() - started:5.1f}s rows={rows:<4} {first[:90]}")
        if state != "SUCCEEDED":
            failed += 1
            print(f"    {res['status'].get('error', {}).get('message', '')[:500]}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
