"""Pre-commit hook: fail if a staged file contains a private term.

Terms come from (1) values of PRIVATE keys in the gitignored .env and
(2) one term per line in the gitignored .private-terms.txt (customer names, system names, ...).
Matching is case-insensitive. Nothing private is ever stored in the repo itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ENV_KEYS = {
    "TENANT_ID", "TENANT_DOMAIN", "ADMIN_UPN", "AZURE_SUBSCRIPTION_ID",
    "SP_CLIENT_ID", "SP_OBJECT_ID", "GITHUB_FABRIC_PAT",
}
PLACEHOLDERS = {"", "00000000-0000-0000-0000-000000000000", "contoso.onmicrosoft.com",
                "admin@contoso.onmicrosoft.com"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".parquet"}


def load_terms() -> set[str]:
    terms: set[str] = set()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if key.strip() in PRIVATE_ENV_KEYS and value not in PLACEHOLDERS:
                    terms.add(value)
                    if "@" in value:
                        terms.add(value.split("@", 1)[1])
                    if value.endswith(".onmicrosoft.com"):
                        terms.add(value.split(".", 1)[0])
    extra = ROOT / ".private-terms.txt"
    if extra.exists():
        terms |= {t.strip() for t in extra.read_text(encoding="utf-8").splitlines()
                  if t.strip() and not t.startswith("#")}
    return {t.lower() for t in terms if len(t) >= 4}


def main(paths: list[str]) -> int:
    terms = load_terms()
    if not terms:
        return 0
    bad = 0
    for p in paths:
        path = Path(p)
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for n, line in enumerate(text.splitlines(), 1):
            if any(t in line for t in terms):
                # Never echo the term itself: CI/terminal logs may be shared.
                print(f"{p}:{n}: contains a private term (from .env/.private-terms.txt)")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
