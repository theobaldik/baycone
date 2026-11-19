import logging
import logging.config
from copy import deepcopy

import colorlog

LEVEL_ABBREVIATIONS = {
    "DEBUG": "DBG",
    "INFO": "INF",
    "WARNING": "WRN",
    "ERROR": "ERR",
    "CRITICAL": "CRT",
}


class LevelFormatter(colorlog.ColoredFormatter):
    # A record attribute of its own: log_colors is keyed by the full levelname
    def format(self, record: logging.LogRecord) -> str:
        record.levelabbr = LEVEL_ABBREVIATIONS.get(record.levelname, record.levelname)
        return super().format(record)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "colored": {
            "()": LevelFormatter,
            "format": "%(log_color)s%(asctime)s.%(msecs)03d [%(levelabbr)s] (%(name)s): %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "log_colors": {
                "DEBUG": "white",
                "INFO": "light_white",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "DEBUG",
            "formatter": "colored",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {"level": "DEBUG", "handlers": ["console"]},
}


def setup_logging(name: str | None = None) -> None:
    """
    Configures the root logger.

    :param name: tags every record, telling apart the jobs that log at the same time
    """
    logging_config = deepcopy(LOGGING_CONFIG)
    if name:
        colored = logging_config["formatters"]["colored"]
        colored["format"] = colored["format"].replace("[%(levelabbr)s]", f"[%(levelabbr)s] [{name}]")
    logging.config.dictConfig(logging_config)
