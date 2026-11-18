"""VorteX AI desktop launcher.

    python app.py

The launcher itself lives in ``app/desktop.py`` so the packaged build can
import it as part of the package.
"""

import sys

from app.desktop import main

if __name__ == "__main__":
    sys.exit(main())
