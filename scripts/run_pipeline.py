#!/usr/bin/env python3
"""Backward-compatibility alias for run_ghpr_analysis.py.

Use ``scripts/run_ghpr_analysis.py`` directly for new invocations.
This wrapper exists only so that any cached shell aliases or CI configs
pointing at ``run_pipeline.py`` continue to work unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_ghpr_analysis import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
