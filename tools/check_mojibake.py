"""Pre-commit hook: block UTF-8 text that was re-decoded as cp1252 ("mojibake", e.g. a check mark shown as 3 odd chars).

A line is flagged when all its characters map to cp1252 bytes AND those bytes are valid UTF-8 that decodes to
something different - i.e. the line is reversible mojibake. Legitimate non-ASCII text never satisfies both.
"""
import sys
from pathlib import Path


def _to_cp1252_bytes(s: str) -> bytes:
    out = bytearray()
    for c in s:
        try:
            out += c.encode("cp1252")
        except UnicodeEncodeError:
            if ord(c) < 256:
                out.append(ord(c))
            else:
                raise
    return bytes(out)


def main(paths: list[str]) -> int:
    bad = 0
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if line.isascii():
                continue
            try:
                if _to_cp1252_bytes(line).decode("utf-8") != line:
                    print(f"{p}:{n}: looks like mojibake (UTF-8 decoded as cp1252); re-save the file as UTF-8")
                    bad += 1
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
