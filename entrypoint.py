import os
import sys

from acervo.db import initialize
from acervo.jobs import interrupted

initialize()
interrupted()
os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.maxUploadSize=200"])
