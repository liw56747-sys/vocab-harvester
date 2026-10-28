"""Repeat pressure scenarios in one process to expose residual state/resource leaks."""
import argparse
import gc
import json
import os
from pathlib import Path
import sys
import time
import threading
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser()
parser.add_argument('--rounds',type=int,default=10)
parser.add_argument('--report',required=True)
args=parser.parse_args()
os.chdir(ROOT)
results=[]
for index in range(args.rounds):
    start=time.monotonic()
    status=pytest.main(['tests/stress/test_release_pressure.py','tests/stress/test_comment_recovery_pressure.py','tests/stress/test_image_pressure.py','tests/test_image_collection.py','tests/test_image_pipeline.py','tests/test_comment_efficiency.py','tests/test_comment_queue.py','-q','--disable-warnings'])
    gc.collect()
    try:
        import resource
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb=peak/(1024*1024 if sys.platform=='darwin' else 1024)
    except ImportError:
        peak_mb=None
    results.append({'round':index+1,'exit_code':int(status),'seconds':round(time.monotonic()-start,2),
                    'process_peak_rss_mb':round(peak_mb,1) if peak_mb else None,
                    'thread_count':threading.active_count(),
                    'open_fds':len(os.listdir('/dev/fd')) if Path('/dev/fd').is_dir() else None})
    Path(args.report).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SOAK_ROUND',json.dumps(results[-1]),flush=True)
    if status:sys.exit(int(status))
