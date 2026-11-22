"""Double-click to start TeXclip without installing it (needs Python + a TeX distribution)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from texclip.gui import main  # noqa: E402

main()
