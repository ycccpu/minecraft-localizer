from pathlib import Path
import multiprocessing
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mc_localizer.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
