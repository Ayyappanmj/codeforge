import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
OUT_ZIP = PARENT / "codeforge-code-execution-sandbox.zip"

EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".idea",
    ".vscode",
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".db",
    ".db-wal",
    ".db-shm",
}

EXCLUDE_FILES = {
    ".env",
    "shot.png",
}

def make_archive():
    print(f"Creating archive at {OUT_ZIP} from {ROOT}...")
    count = 0
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(ROOT):
            # Prune excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            rel_root = Path(root).relative_to(ROOT.parent)

            for file in files:
                if file in EXCLUDE_FILES:
                    continue
                ext = Path(file).suffix
                if ext in EXCLUDE_EXTS:
                    continue

                full_path = Path(root) / file
                arcname = str(rel_root / file).replace("\\", "/")
                zf.write(full_path, arcname)
                count += 1

    size = os.path.getsize(OUT_ZIP)
    print(f"Archived {count} files into {OUT_ZIP} ({size:,} bytes).")

if __name__ == "__main__":
    make_archive()
