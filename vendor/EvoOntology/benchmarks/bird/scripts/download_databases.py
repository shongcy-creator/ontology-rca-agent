#!/usr/bin/env python3
"""Download and extract BIRD Mini-Dev SQLite databases.

Source: BIRD official OSS (https://bird-bench.github.io/)
Target: EvoOntology/bird/data/mini_dev_data/dev_databases/

Usage:
    python scripts/download_databases.py              # download + extract
    python scripts/download_databases.py --keep-zip   # keep the zip after extraction
    python scripts/download_databases.py --force      # re-download even if databases exist
"""

import os
import sys
import zipfile
import hashlib
import urllib.request
from pathlib import Path

URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
ZIP_NAME = "minidev.zip"

# 11 expected databases
EXPECTED_DBS = [
    "california_schools", "card_games", "codebase_community",
    "debit_card_specializing", "european_football_2", "financial",
    "formula_1", "student_club", "superhero",
    "thrombosis_prediction", "toxicology",
]


def _bird_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _check_existing(target_dir: Path) -> list[str]:
    """Return list of databases whose .sqlite files already exist."""
    missing = []
    for db in EXPECTED_DBS:
        sqlite_path = target_dir / db / f"{db}.sqlite"
        if not sqlite_path.exists():
            missing.append(db)
    return missing


def _download(url: str, dest: Path) -> None:
    """Download file with progress display."""
    print(f"下载中: {url}")
    print(f"保存到: {dest}")

    def _progress(block_count, block_size, total_size):
        if total_size <= 0:
            return
        downloaded = min(block_count * block_size, total_size)
        pct = downloaded / total_size * 100
        mb_down = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        bar_len = 40
        filled = int(bar_len * downloaded / total_size)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {pct:5.1f}%  {mb_down:.0f}/{mb_total:.0f} MB", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=_progress)
    print()  # newline after progress bar


def _extract_sqlite(zip_path: Path, target_dir: Path) -> int:
    """Extract only .sqlite files from the zip, stripping any top-level prefix."""
    print(f"解压 .sqlite 文件到: {target_dir}")
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if not member.endswith(".sqlite"):
                continue
            # Normalize: handle various zip layouts
            # minidev.zip 可能是 dev_databases/<db>/<db>.sqlite 或带前缀
            parts = Path(member).parts
            # Find the <db_name>/<db_name>.sqlite pattern
            db_name = None
            for i, p in enumerate(parts):
                if p in EXPECTED_DBS:
                    db_name = p
                    break
            if db_name is None:
                print(f"  跳过无法识别的文件: {member}")
                continue

            dest = target_dir / db_name / f"{db_name}.sqlite"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(f"  已存在，跳过: {db_name}/{db_name}.sqlite")
                count += 1
                continue

            print(f"  解压: {db_name}/{db_name}.sqlite")
            # Extract to a temp path then rename to avoid partial writes
            tmp = dest.with_suffix(".tmp")
            with zf.open(member) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(8 * 1024 * 1024)  # 8MB chunks for large files
                    if not chunk:
                        break
                    dst.write(chunk)
            tmp.rename(dest)
            count += 1
    return count


def main():
    keep_zip = "--keep-zip" in sys.argv
    force = "--force" in sys.argv

    root = _bird_root()
    target_dir = root / "data" / "mini_dev_data" / "dev_databases"
    zip_dest = root / ZIP_NAME

    if not force:
        missing = _check_existing(target_dir)
        if not missing:
            print("全部 11 个数据库已就绪，无需下载。")
            print("(使用 --force 强制重新下载)")
            return

        print(f"缺失 {len(missing)} 个数据库: {', '.join(missing)}")
        if len(missing) < 11:
            print(f"已有 {11 - len(missing)} 个，将仅解压缺失的。")
    else:
        print("--force: 将重新下载并覆盖已有数据库")

    # Download
    if zip_dest.exists() and not force:
        print(f"\n发现已有 {ZIP_NAME}，跳过下载（使用 --force 重新下载）")
    else:
        _download(URL, zip_dest)

    # Verify zip integrity
    print("校验 zip 文件...")
    try:
        with zipfile.ZipFile(zip_dest, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                print(f"zip 文件损坏: {bad}")
                print("删除损坏的 zip，请重新运行。")
                zip_dest.unlink()
                sys.exit(1)
    except Exception as e:
        print(f"zip 文件无效: {e}")
        zip_dest.unlink()
        sys.exit(1)
    print("  OK")

    # Extract
    count = _extract_sqlite(zip_dest, target_dir)
    print(f"\n解压完成: {count} 个数据库")

    # Cleanup
    if not keep_zip:
        zip_dest.unlink()
        print(f"已删除 {ZIP_NAME}（使用 --keep-zip 保留）")
    else:
        print(f"保留 {ZIP_NAME}")

    # Verify final state
    missing = _check_existing(target_dir)
    if missing:
        print(f"\n警告: {len(missing)} 个数据库仍然缺失: {', '.join(missing)}")
        sys.exit(1)
    else:
        print("\n全部 11 个数据库就绪。")


if __name__ == "__main__":
    main()
