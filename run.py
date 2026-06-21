import subprocess
import sys
from pathlib import Path


SCRIPTS = [
    "harvard.py",
    "calcutta_scraper_new.py",
    "iitkgp.py",
    "bhu.py"
]


def run_script(script_name):
    print("\n" + "=" * 80)
    print(f"Running: {script_name}")
    print("=" * 80)

    result = subprocess.run(
        [sys.executable, script_name],
        capture_output=False,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"{script_name} failed with exit code {result.returncode}"
        )

    print(f"\n✓ Completed: {script_name}")


def main():
    print("Starting Faculty Scraper Pipeline")

    missing = [s for s in SCRIPTS if not Path(s).exists()]
    if missing:
        print("\nMissing files:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)

    for script in SCRIPTS:
        run_script(script)

    print("\n" + "=" * 80)
    print("ALL SCRAPERS COMPLETED SUCCESSFULLY")
    print("=" * 80)

    print("\nGenerated files:")
    print("  - harvard_faculty_clean.csv")
    print("  - calcutta_faculty_optimized.csv")
    print("  - iitkgp_faculty.csv")
    print("  - bhu_faculty.csv")


if __name__ == "__main__":
    main()