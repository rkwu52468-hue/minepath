"""Run the fixed 25 business tests plus frozen exact track-response cases."""
from pathlib import Path
import os
import subprocess
import sys
import time
PROJECT = Path(__file__).resolve().parents[1]
output = PROJECT / '.cache/autoresearch-tracks'
output.mkdir(parents=True, exist_ok=True)
stamp = str(time.time_ns())
temp_root = PROJECT / '.cache' / 'tmp'
temp_root.mkdir(parents=True, exist_ok=True)
environment = os.environ.copy()
environment['PYTHONUTF8'] = '1'
environment['PYTHONDONTWRITEBYTECODE'] = '1'
command = [sys.executable, '-B', '-X', 'utf8', '-m', 'pytest', 'tests/test_system.py', 'tests/test_tracks_contract.py', '-q', '-p', 'no:cacheprovider', '--basetemp=' + str(temp_root / ('track-guard-' + stamp)), '--junitxml=' + str(output / ('guard-' + stamp + '.xml'))]
raise SystemExit(subprocess.call(command, cwd=PROJECT, env=environment))
