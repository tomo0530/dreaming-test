"""Make the shared test helpers in this directory (`fakes`) importable from subpackages."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
