"""Development launcher; install the package to use the `mc-localizer` command."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mc_localizer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

