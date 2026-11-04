"""V6.0 read-only diagnostics. Never changes caches, scores, or scans.

Run:
  python tools/v6_healthcheck.py --root . --json
  python tools/v6_healthcheck.py --root . --live --json

Live checks make one GET request to each public endpoint only when explicitly enabled.
"""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

REQUIRED = (
    "v52_test/app_v52_test.py",
    "v52_test/app_v52.py",
    "v52_test/v52_data.py",
    "v52_test/v52_bulk.py",
    "v52_test/v52_cache.py",
    "v52_test/v52_backtest.py",
    "v52_test/v52_engine.py",
    "v52_test/v52_config.py",
    "requirements.txt",
)
# Explicit endpoints: no user-supplied URLs, credentials, or response bodies printed.
ENDPOINTS = {
    "twse_quotes": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "tpex_quotes": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    "yahoo_spy_chart": "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=5d&interval=1d",
}


def classify_http(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "ok"


def inspect_source(root: Path) -> list[dict]:
    findings = []
    for name in REQUIRED:
        p = root / name
        if not p.is_file():
            findings.append({"check": name, "status": "fail", "reason": "missing"})
            continue
        if p.suffix == ".py":
            try:
                ast.parse(p.read_text(encoding="utf-8"), filename=name)
            except (SyntaxError, UnicodeError) as exc:
                findings.append({"check": name, "status": "fail",
                                 "reason": type(exc).__name__})
                continue
        findings.append({"check": name, "status": "pass"})
    return findings


def check_dependencies(requirements: Path) -> list[dict]:
    findings = []
    if not requirements.exists():
        return [{"check": "dependencies", "status": "fail", "reason": "requirements missing"}]
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = line.split("==", 1)[0].strip()
        try:
            version = importlib.metadata.version(name)
            specified = line.split("==", 1)[1] if "==" in line else None
            status = "pass" if specified is None or version == specified else "warn"
            findings.append({"check": "dependency:" + name, "status": status,
                             "installed": version, "required": specified})
        except importlib.metadata.PackageNotFoundError:
            findings.append({"check": "dependency:" + name, "status": "warn",
                             "reason": "not installed in diagnostic environment"})
    return findings


def probe_url(url: str, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": "QuantStockRadar-Healthcheck/6.0",
                      "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # A successful HTTP status does not prove schema or freshness.
            return {"status": "reachable", "http_status": status,
                    "category": classify_http(status)}
    except urllib.error.HTTPError as exc:
        return {"status": "unavailable", "http_status": exc.code,
                "category": classify_http(exc.code)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "unavailable", "category": "network_error",
                "error_type": type(exc).__name__}


def run(root: Path, live: bool = False, dependencies: bool = False) -> dict:
    checks = inspect_source(root)
    if dependencies:
        checks.extend(check_dependencies(root / "requirements.txt"))
    if live:
        for name, url in ENDPOINTS.items():
            result = probe_url(url)
            checks.append({"check": "source:" + name,
                           "status": "pass" if result["status"] == "reachable" else "warn",
                           **result})
    failed = sum(x["status"] == "fail" for x in checks)
    warned = sum(x["status"] == "warn" for x in checks)
    return {"tool": "v6_healthcheck", "mode": "live" if live else "offline",
            "checks": checks, "failures": failed, "warnings": warned,
            "note": "Static syntax and optional HTTP reachability only; not an end-to-end scan or data accuracy test."}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--live", action="store_true", help="Explicitly probe 3 external endpoints")
    p.add_argument("--dependencies", action="store_true", help="Check installed package versions")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    result = run(args.root, args.live, args.dependencies)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for c in result["checks"]:
            print(f"{c['status'].upper():4} {c['check']} {c.get('category', c.get('reason',''))}")
        print(f"failures={result['failures']} warnings={result['warnings']}")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
