import logging.config
from copy import deepcopy

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "colored": {
            "()": "colorlog.ColoredFormatter",
            "format": "%(log_color)s%(asctime)s.%(msecs)03d [%(levelname)s] (%(name)s): %(message)s",
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
        colored["format"] = colored["format"].replace("[%(levelname)s]", f"[%(levelname)s] [{name}]")
    logging.config.dictConfig(logging_config)
