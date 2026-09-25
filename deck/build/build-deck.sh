#!/bin/bash
# Build the Night's Watch deck: generate HTML, print with Chrome, check pages and fonts.
set -euo pipefail
D=/Users/vroy/Developer/projects/sept25-build/deck
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
N=5

python3 "$D/build/gen_deck.py"
cd "$D"
"$CHROME" --headless --disable-gpu --no-pdf-header-footer --virtual-time-budget=10000 \
  --print-to-pdf="$D/nights-watch.pdf" "file://$D/nights-watch.html" 2>/dev/null

python3 - "$D/nights-watch.pdf" "$N" <<'EOF'
import sys
d = open(sys.argv[1], "rb").read()
n = d.count(b"/Type /Page") - d.count(b"/Type /Pages")
print("pages", n)
assert n == int(sys.argv[2]), f"expected {sys.argv[2]} pages, got {n}"
EOF
for p in $(seq 1 $N); do
  [ -n "$(pdftotext -f $p -l $p "$D/nights-watch.pdf" - | tr -d '[:space:]')" ] || { echo "blank page $p"; exit 1; }
done
pdffonts "$D/nights-watch.pdf" | tail -n +3 | awk '{print "font", $1}' | sort -u
