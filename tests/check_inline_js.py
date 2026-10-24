#!/usr/bin/env python3
"""Синтаксическая проверка inline-JS в HTML (приём из CLAUDE.md: node -e "new Function(...)").

Использование: python3 tests/check_inline_js.py web-orchestrator/static/*.html
Извлекает <script> без src и проверяет каждый блок через `new Function` в node.
Код не выполняется — только парсится. Ненулевой код выхода при ошибке.
"""
import re
import subprocess
import sys

SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
NODE_CHECK = (
    "const fs=require('fs');"
    "try{new Function(fs.readFileSync(0,'utf8'));}"
    "catch(e){console.error(e.name+': '+e.message);process.exit(1);}"
)


def main(paths):
    failed = 0
    checked = 0
    for path in paths:
        html = open(path, encoding="utf-8").read()
        for m in SCRIPT_RE.finditer(html):
            attrs = m.group("attrs")
            if re.search(r"\bsrc\s*=", attrs) or re.search(r"type\s*=\s*['\"]?(module|application/json|text/template)", attrs):
                continue
            body = m.group("body")
            if not body.strip():
                continue
            line = html.count("\n", 0, m.start()) + 1
            checked += 1
            r = subprocess.run(["node", "-e", NODE_CHECK], input=body, text=True, capture_output=True)
            if r.returncode != 0:
                failed += 1
                print(f"FAIL {path}:{line}: {r.stderr.strip()}")
            else:
                print(f"ok   {path}:{line} ({len(body)} байт)")
    if checked == 0:
        print("Не найдено ни одного inline <script> — проверь пути")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
