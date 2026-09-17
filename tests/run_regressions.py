"""Run existing regression checks in a disposable copy of the plugin."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    checks = [
        ("test_update_config.py", "UPDATE_RESULTS=", False),
        ("test_database_fix.py", "TEST_RESULTS=", True),
        ("test_rift_rewards.py", "TEST_RESULTS=", True),
        ("test_inventory.py", "AUDIT_RESULTS=", False),
        ("test_alchemy.py", "ALCHEMY_RESULTS=", False),
    ]
    with tempfile.TemporaryDirectory(prefix="xiuxian-regressions-") as directory:
        copied = Path(directory) / "astrbot_plugin_monixiuxian2"
        shutil.copytree(root, copied, ignore=shutil.ignore_patterns(
            ".git", ".serena", "__pycache__", "*.pyc", ".venv", "venv", ".env*",
            "*.db", "*.db-*", "*.sqlite*", "*.log"))
        results = {}
        for filename, marker, named_root in checks:
            args = ["--plugin-root", str(copied)] if named_root else [str(copied)]
            result = subprocess.run([sys.executable, "-B", str(copied / "tests" / filename), *args],
                                    cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=180)
            if result.returncode:
                print(result.stdout, flush=True)
                print(result.stderr, file=sys.stderr, flush=True)
                return result.returncode
            summaries = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
            if len(summaries) != 1:
                raise RuntimeError("Missing check summary: " + filename)
            results[filename] = json.loads(summaries[0])
            print(f"PASS {filename}: {len(results[filename])} result entries", flush=True)
        print("REGRESSION_RESULTS=" + json.dumps(results), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
