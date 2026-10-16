"""Fail-fast cross-platform release UI gate; every script must return success."""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
for name,args in [
    ('check_collection_controls.py',[]),
    ('check_result_summary.py',[]),
    ('check_desktop_navigation.py',['--iterations','1400']),
    ('check_update_ui.py',[]),
    ('check_comment_recovery_ui.py',[]),
]:
    print(f'Checking {name}',flush=True)
    subprocess.run([sys.executable,str(ROOT/'tests/manual'/name),*args],cwd=ROOT,check=True,timeout=180)
print('PASS: all five release UI checks')
