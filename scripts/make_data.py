"""Copy the sales warehouse and its dbt artifacts into ./data (gitignored).

Source: the sibling project `sales-analytics-platform` (folder 05 of the career pack), which
is treated as READ-ONLY. Nothing is modified there; files are copied and their sha256 is
recorded in data/PROVENANCE.json so every number in reports/ can be traced to the exact
warehouse file it was computed on.

    python scripts/make_data.py                      # default source: ../05_sales-analytics-platform
    python scripts/make_data.py --source /path/to/sales-analytics-platform
    SALES_PLATFORM_DIR=/path python scripts/make_data.py

Idempotent: a file whose size and sha256 already match is not copied again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "05_sales-analytics-platform"

# (relative path in the source project, relative path under data/)
WAREHOUSE_FILES = [
    ("data/warehouse/sales_real_replay.duckdb", "warehouse/sales_real_replay.duckdb"),
    ("data/warehouse/sales_real.duckdb", "warehouse/sales_real.duckdb"),
]
DBT_FILES = [
    ("warehouse/target/manifest.json", "dbt/manifest.json"),
    ("warehouse/target/catalog.json", "dbt/catalog.json"),
    ("warehouse/dbt_project.yml", "dbt/dbt_project.yml"),
]
DBT_YAML_GLOB = "warehouse/models/**/*.yml"


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def copy_one(src: Path, dst: Path, previous: dict | None) -> dict:
    if not src.exists():
        raise FileNotFoundError(f"missing source file: {src}")
    src_hash = sha256(src)
    if (
        dst.exists()
        and previous
        and previous.get("sha256") == src_hash
        and dst.stat().st_size == src.stat().st_size
    ):
        status = "unchanged"
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp)
        if sha256(tmp) != src_hash:
            tmp.unlink(missing_ok=True)
            raise OSError(f"checksum mismatch after copying {src}")
        os.replace(tmp, dst)  # atomic on the same volume
        status = "copied"
    return {
        "source": str(src),
        "path": dst.relative_to(ROOT).as_posix(),
        "bytes": src.stat().st_size,
        "sha256": src_hash,
        "source_mtime": datetime.fromtimestamp(src.stat().st_mtime, UTC).isoformat(),
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.environ.get("SALES_PLATFORM_DIR", DEFAULT_SOURCE)),
        help="root of the sales-analytics-platform checkout",
    )
    parser.add_argument("--dest", type=Path, default=ROOT / "data")
    args = parser.parse_args(argv)

    source: Path = args.source.resolve()
    dest: Path = args.dest.resolve()
    if not source.exists():
        print(f"error: source project not found: {source}", file=sys.stderr)
        return 2

    provenance_path = dest / "PROVENANCE.json"
    previous: dict[str, dict] = {}
    if provenance_path.exists():
        previous = {
            f["path"]: f for f in json.loads(provenance_path.read_text(encoding="utf-8"))["files"]
        }

    pairs = [(source / s, dest / d) for s, d in WAREHOUSE_FILES + DBT_FILES]
    for yml in sorted(source.glob(DBT_YAML_GLOB)):
        rel = yml.relative_to(source / "warehouse")
        pairs.append((yml, dest / "dbt" / rel))

    files = []
    for src, dst in pairs:
        rel = dst.relative_to(ROOT).as_posix()
        record = copy_one(src, dst, previous.get(rel))
        files.append(record)
        print(f"{record['status']:>9}  {rel}  ({record['bytes'] / 1e6:.1f} MB)")

    provenance = {
        "description": (
            "Copied from the sales-analytics-platform project (read-only source). "
            "UCI Online Retail II data (CC BY 4.0), transformed by that project's dbt models."
        ),
        "source_root": str(source),
        "copied_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    dest.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(f"wrote {provenance_path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
