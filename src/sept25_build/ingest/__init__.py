"""Lane A: ingest (Nimble). Owner: see AGENTS.md "Lanes".

Produces contracts.Snapshot objects and appends them to nw_snapshots.
Interface the other lanes rely on:
    fetch_vendor(vendor: str) -> Snapshot
    run_once() -> list[Snapshot]      # fetch every vendor, insert into nw_snapshots, return them
"""
