"""保留既有 Streamlit Cloud 入口檔名；V5.1 原入口另存 app_v51.py。"""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).with_name('app_v52.py')),run_name='__main__')
