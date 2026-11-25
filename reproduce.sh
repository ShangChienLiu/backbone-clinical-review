#!/bin/sh
set -eu
cd "$(dirname "$0")"
dest=${1:-reproduced}
mkdir -p "$dest"
if [ -e "$dest/rowan.sqlite" ] || [ -e "$dest/evaluation.sqlite" ]; then
    printf '%s\n' "Choose a fresh output directory for a clean run." >&2
    exit 2
fi
python3 clinical.py --db "$dest/rowan.sqlite" ingest documents > "$dest/rowan-ingest.json"
python3 clinical.py --db "$dest/rowan.sqlite" ingest documents > "$dest/rowan-cached-ingest.json"
python3 clinical.py --db "$dest/rowan.sqlite" answer questions.json > "$dest/development-answers.json"
python3 clinical.py --db "$dest/rowan.sqlite" export > "$dest/rowan-abstraction.json"
python3 clinical.py --db "$dest/evaluation.sqlite" ingest evaluation-documents > "$dest/evaluation-ingest.json"
python3 clinical.py --db "$dest/evaluation.sqlite" ask 'Which patients had two consecutive weeks below their treatment plan requirements, March 2–29, 2026?' > "$dest/collection-review.json"
python3 clinical.py --db "$dest/evaluation.sqlite" ask 'How did Theo’s therapy change before and after his treatment plan change, March 2–29, 2026?' > "$dest/plan-change.json"
python3 clinical.py --db "$dest/evaluation.sqlite" ask 'How did Nadia’s symptoms progress from February 20, 2026 to March 29, 2026?' > "$dest/evaluation-symptoms.json"
python3 clinical.py --db "$dest/combined.sqlite" ingest documents > "$dest/combined-development-ingest.json"
python3 clinical.py --db "$dest/combined.sqlite" ingest evaluation-documents > "$dest/combined-example-ingest.json"
python3 clinical.py --db "$dest/combined.sqlite" ask 'Which patients had two consecutive weeks below their plan from January 5, 2026 to March 29, 2026?' > "$dest/combined-collection-review.json"
python3 -m unittest discover -s tests -v
python3 edge_suite.py > "$dest/edge-evaluation.json"
python3 experiment.py --repetitions 20 > "$dest/benchmarks.json"
python3 scale.py > "$dest/synthetic-scale.json"
printf 'Review and benchmark outputs saved in %s\n' "$dest"
