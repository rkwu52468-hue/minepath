from __future__ import annotations
import argparse,atexit,os,signal,sys,threading
from pathlib import Path

PROJECT=Path(__file__).resolve().parent
sys.path.insert(0,str(PROJECT/'src'))
from minepath import create_app
from minepath.management import run_background

def main():
    parser=argparse.ArgumentParser(description='矿迹 · 人员轨迹监控与可视化系统')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--db',type=Path)
    parser.add_argument('--demo',action='store_true',help='首次运行导入1万条已标注的模拟历史')
    parser.add_argument('--tls',action='store_true',help='使用data/tls中的本地证书启动HTTPS')
    parser.add_argument('--init-only',action='store_true')
    args=parser.parse_args()
    app=create_app(PROJECT,args.db); svc=app.extensions['service']
    if args.demo:
        with svc.tx() as db: empty=db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
        source=PROJECT/'evidence'/'simulated'/'sim-20261028-v1'/'positions_10000.csv'
        if empty and source.exists(): print('Imported simulated observations:',svc.import_simulation(source,'ADMIN'),flush=True)
    if args.init_only: return
    stop=threading.Event(); atexit.register(stop.set)
    thread=threading.Thread(target=run_background,args=(app,stop),daemon=True); thread.start()
    print(f'MinePath local server: {"https" if args.tls else "http"}://{args.host}:{args.port}',flush=True)
    print('Demo users: admin / leader01..03 / employee01..30. Initial demo password: environment MINEPATH_DEMO_PASSWORD or Demo@2026!',flush=True)
    try:
        if args.tls:
            cert=PROJECT/'data'/'tls'/'server.pem'; key=PROJECT/'data'/'tls'/'server-key.pem'
            if not cert.exists(): raise SystemExit('Run tools/create_local_tls.py first. Trust the generated local CA on the test device before using mobile Geolocation.')
            app.config['SESSION_COOKIE_SECURE']=True
            app.run(host=args.host,port=args.port,threaded=True,debug=False,use_reloader=False,ssl_context=(str(cert),str(key)))
        else:
            from waitress import serve
            serve(app,host=args.host,port=args.port,threads=48,channel_timeout=60)
    finally: stop.set()

if __name__=='__main__': main()
