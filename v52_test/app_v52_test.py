"""僅供upgrade/v5.2獨立Streamlit測試站的明確入口。"""
import os
import runpy
from pathlib import Path
# Opt-in diagnostic branch; default execution remains the original app.
import streamlit as st
if st.query_params.get('v53_official_audit') == '1':
    from v53_official_acceptance import main as run_official_acceptance
    run_official_acceptance()
    st.stop()
os.environ['V52_TEST_SITE']='1'
runpy.run_path(str(Path(__file__).with_name('app_v52.py')),run_name='__main__')
