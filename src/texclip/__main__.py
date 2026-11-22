"""``python -m texclip`` opens the GUI; ``python -m texclip --cli ...`` or the ``texclip`` command is the CLI."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    from .cli import main
    main(sys.argv[2:])
else:
    from .gui import main
    main()
