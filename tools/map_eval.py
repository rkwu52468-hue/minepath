"""Frozen 12-case functionality metric; failed assertions are valid poor scores."""
from pathlib import Path
import json,os,subprocess,sys,time,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
out=ROOT/'.cache/autoresearch-maps';out.mkdir(parents=True,exist_ok=True)
stamp=str(time.time_ns());xml=out/('acceptance-'+stamp+'.xml')
cmd=[sys.executable,'-B','-X','utf8','-m','pytest','tests/test_site_maps.py','-q','-p','no:cacheprovider','--basetemp='+str(out/('tmp-'+stamp)),'--junitxml='+str(xml)]
r=subprocess.run(cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'})
(out/('acceptance-'+stamp+'.log')).write_text(r.stdout,encoding='utf-8')
if r.returncode not in (0,1) or not xml.exists():
    print(r.stdout);raise SystemExit(r.returncode or 2)
s=ET.parse(xml).getroot().find('testsuite')
count=int(s.attrib['tests']);errors=int(s.attrib.get('errors',0));skips=int(s.attrib.get('skipped',0));failures=int(s.attrib.get('failures',0))
if count!=12 or errors or skips:print(r.stdout);raise SystemExit('Invalid measurement: require 12 collected tests, no setup errors or skips')
print(json.dumps({'passed':count-failures,'total':12,'failures':failures,'xml':str(xml),'log':str(out/('acceptance-'+stamp+'.log'))}))
