# logging_config.py
import logging
from pathlib import Path
from datetime import datetime

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

log_file = LOGS_DIR / f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

log_format = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger('avito_bot')
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_format)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_format)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

logging.getLogger('aiogram').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

def get_logger(name: str = 'avito_bot') -> logging.Logger:
    return logging.getLogger(name)