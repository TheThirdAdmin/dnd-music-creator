import logging
import sys

import uvicorn

from .app import build_services, check_library, create_app
from .config import Settings
from .freetext import load_rules
from .library import Library
from .vocab import VocabError, load_vocab


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    try:
        check_library(Library(settings.data_dir), load_vocab(settings.data_dir))
        load_rules(settings.data_dir)
    except VocabError as e:
        sys.exit(f"dnd-music: {e}. Fix it (or delete the file to get the defaults back) and restart.")
    logging.getLogger(__name__).info("stream at %s/stream.mp3, generators %s",
                                     settings.public_url, ", ".join(settings.generator_urls))
    uvicorn.run(create_app(lambda: build_services(settings)), host="0.0.0.0", port=settings.port,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
