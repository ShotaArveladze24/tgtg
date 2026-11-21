"""Entry point: python -m taskbot"""

import logging
import os

from .bot import build_application
from .config import Config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("TASKBOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application(Config.from_env())
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
