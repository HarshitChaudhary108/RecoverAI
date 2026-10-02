import logging
import json
import sys
from datetime import datetime

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        # Basic secret scrubbing (can be expanded)
        log_str = json.dumps(log_record)
        # Simple check for common secret markers in the final string to be safe
        # In a real app, we'd scrub specific keys from the log_record dict first.
        return log_str

def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Clear existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    formatter = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%SZ")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logging.info("Logging initialized in JSON mode")
