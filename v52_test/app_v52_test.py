"""僅供upgrade/v5.2獨立Streamlit測試站的明確入口。"""
import os
import runpy
from pathlib import Path
os.environ['V52_TEST_SITE']='1'
runpy.run_path(str(Path(__file__).with_name('app_v52.py')),run_name='__main__')
