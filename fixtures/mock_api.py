import argparse,json
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlsplit,unquote,unquote_plus
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def serve(self):
  u=urlsplit(self.path);parts=[x.partition('=') for x in u.query.split('&') if x]; pairs=[(unquote_plus(k),unquote_plus(v)) for k,_,v in parts];out={'raw_target':self.path,'method':self.command};status=200
  if self.command=='GET' and u.path.startswith('/assets/'):
   out['asset_id']=unquote(u.path[len('/assets/'):])
  elif self.command=='GET' and u.path=='/search':
   out['tags']=[];out['filter']={}
   for k,_,v in parts:
    k=unquote_plus(k)
    if k=='tags':out['tags']=[unquote_plus(a) for a in v.split(',')] if v else []
    elif k=='active':out['active']=unquote_plus(v)
    elif k.startswith('filter[') and k.endswith(']'):out['filter'][k[7:-1]]=unquote_plus(v)
    else:status=400;out['error']='unknown parameter'
  elif self.command=='GET' and u.path=='/labels':out['pairs']=pairs
  elif self.command=='POST' and u.path=='/reports':
   out['pairs']=pairs
   try:out['body']=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
   except Exception:status=400;out['error']='invalid JSON'
  else:status=404;out['error']='not found'
  b=json.dumps(out,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 do_GET=serve
 do_POST=serve
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8171);a=p.parse_args();HTTPServer(('127.0.0.1',a.port),H).serve_forever()
