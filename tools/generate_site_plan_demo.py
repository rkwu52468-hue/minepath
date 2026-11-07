"""Generate a deterministic, explicitly simulated site-plan upload fixture."""
from pathlib import Path
import hashlib,json
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
out=ROOT/'evidence/simulated/site-map-demo-v1';out.mkdir(parents=True,exist_ok=True)
image=Image.new('RGB',(1200,480),'#eef0e7');d=ImageDraw.Draw(image)
for x in range(0,1200,40):d.line((x,0,x,480),fill='#dce4d3')
for y in range(0,480,40):d.line((0,y,1200,y),fill='#dce4d3')
d.rectangle((0,80,1200,135),fill='#c5cdbf');d.line((0,108,1200,108),fill='white',width=2)
for x,label in [(80,'A WORK AREA'),(480,'B STORAGE'),(880,'C MAINTENANCE')]:
    d.rounded_rectangle((x,160,x+200,360),radius=10,fill='#dce7d1',outline='#6d8d5b',width=3);d.text((x+15,180),label,fill='#345d42')
d.text((30,25),'SIMULATED SITE PLAN - SOFTWARE DEMONSTRATION ONLY',fill='#345d42')
d.text((30,440),'1200 x 480 pixels | width 600m | origin (-40,-60) | NOT A REAL MINE',fill='#345d42')
path=out/'site-plan-demo.png';image.save(path)
meta={'source':'deterministic simulated software fixture','is_simulated':True,'width_px':1200,'height_px':480,'width_m':600,'meters_per_pixel':0.5,'origin_x':-40,'origin_y':-60,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'generator':'tools/generate_site_plan_demo.py','notice':'Not a real mine plan or a measured campus map.'}
(out/'manifest.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(meta,ensure_ascii=False))
