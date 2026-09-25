# Fixtures (synthetic)

All sub-processor names and policy text here are **made up** for testing. They are not real vendor data.

`snapshots/*.json` are `contracts.Snapshot` objects:
- `baseline_<vendor>.json`: one per sponsor vendor, for building the graph before real ingest works.
- `mirror_01..06`: a timeline for the injectable demo mirror (`tinybird-mirror`), in order:
Injections are cumulative (03 → 05 stack up); 06 undoes all of them.

  1. baseline (compliant)
  2. noise: only the "Last updated" date changed. The agent must discard this.
  3. inject: new sub-processor DataHarvest Ltd in Singapore. Violates R1.
  4. inject: policy now allows training on customer data. Violates R2.
  5. inject: retention changed to 24 months. Violates R3.
  6. reset: back to compliant. Open findings should resolve.

Load one: `Snapshot.model_validate_json(Path(p).read_text())`.
