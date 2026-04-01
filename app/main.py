import sys
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from app.ui.window import RecorderApplication
from app.utils.logging import setup_logging


def main() -> int:
    log_path = setup_logging()
    logging.getLogger(__name__).info("Application starting; log file: %s", log_path)
    app = RecorderApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
