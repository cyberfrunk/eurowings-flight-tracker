#!/bin/bash
set -e

REPO="/home/pi/eurowings-flight-tracker"

cd "$REPO"

echo "Aktuelle Produktionsversion übernehmen ..."
cp /home/pi/flug_checker.py "$REPO/flug_checker.py"

if [ -f /home/pi/flug_checker_daily_pdf.py ]; then
    echo "Legacy Daily/PDF-Version übernehmen ..."
    mkdir -p "$REPO/legacy"
    cp /home/pi/flug_checker_daily_pdf.py "$REPO/legacy/flug_checker_daily_pdf.py"
fi

echo "Syntaxcheck aktuelle Version ..."
python3 -m py_compile "$REPO/flug_checker.py"

echo "Git Status:"
git status --short

git add -A

if git diff --cached --quiet; then
    echo "Keine neuen Änderungen"
    exit 0
fi

git commit -m "Update flight checker"
git push

echo "Alles synchron"
