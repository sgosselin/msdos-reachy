#!/usr/bin/env python3
"""Audit files Git would publish, without printing any matched secret values."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "personal filesystem path": re.compile(r"(?:/" + r"Users/|/" + r"home/|[A-Za-z]:\\Users\\)[^\s`\"<>]+"),
    "Muse SDK token": re.compile(r"mgst" + r"_[A-Za-z0-9_-]{40,}"),
    "private key": re.compile(r"-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "JWT credential": re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    "credential assignment": re.compile(r'(?i)["\x27](?:access_token|refresh_token|password)["\x27]\s*:\s*["\x27][^"\x27\s]{8,}["\x27]'),
}
DOC_PATH = re.compile(r"(?:^|[\s`\"(>])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*")


def audit(root=ROOT):
    output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    findings = []
    count = 0
    for relative in sorted(set(output.decode().split("\0")) - {""}):
        path = root / relative
        if not path.exists():
            continue
        count += 1
        if relative.startswith("artifacts/"):
            findings.append((relative, "private artifacts tracked"))
            continue
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".wav", ".mp3", ".pem", ".key"):
            findings.append((relative, "media or key file requires review"))
        try:
            content = path.read_text()
        except UnicodeError:
            findings.append((relative, "binary file requires review"))
            continue
        for category, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((relative, category))
        if path.suffix.lower() in (".md", ".html", ".json") and DOC_PATH.search(content):
            findings.append((relative, "absolute path in documentation or configuration"))
    return count, findings


def main():
    count, findings = audit()
    for path, category in findings:
        print(f"{path}: {category}")
    print(f"Audited {count} public files; {len(findings)} findings.")
    raise SystemExit(bool(findings))


if __name__ == "__main__":
    main()
