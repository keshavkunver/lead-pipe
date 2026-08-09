import sys
from pathlib import Path

# Modules live flat at the project root (no package), so tests need the
# root on sys.path to import webhook/sender/approve/lead_brain.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
