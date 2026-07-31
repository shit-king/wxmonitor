import os
from pathlib import Path


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WeChatVisualMonitor"
DATABASE_PATH = APP_DIR / "wechat_text_monitor.db"
