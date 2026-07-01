"""Make the skill's runtime scripts importable from dev/tests/ (tests live OUTSIDE
the deployable scripts/ surface). dev/tests/ → skill-root/scripts."""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "hooks"))
sys.path.insert(0, str(_SCRIPTS))
