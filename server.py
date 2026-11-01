"""FlipLedger V6 local AI vision backend. Requires OPENAI_API_KEY in environment."""
import base64, json, os, re
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/api/identify':
            self.send_error(404); return
        if not os.environ.get('OPENAI_API_KEY'):
            return self.reply(503, {'error':'AI service not configured. Set OPENAI_API_KEY on the server.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length < 1 or length > 9_000_000: return self.reply(413, {'error':'Image too large (max 6 MB).'})
            obj=json.loads(self.rfile.read(length))
            image=obj.get('image','')
            if not re.match(r'^data:image/(jpeg|png|webp);base64,',image): return self.reply(400, {'error':'Use a JPEG, PNG or WebP image.'})
            if len(image)>8_500_000:return self.reply(413,{'error':'Image too large.'})
            payload={'model':os.environ.get('FLIPLEDGER_VISION_MODEL','gpt-4.1-mini'), 'messages':[{'role':'system','content':'Identify resale items from photos. Never invent editions or condition. Respond ONLY with a JSON object with keys title, category, edition, confidence, notes. Category must be one of DVD / Blu-ray, Video games, Collectibles, Electronics, Clothing & accessories, Other. confidence must be low, medium, or high. Use empty string for unknown title or edition. Title should be concise and suitable for an eBay listing, max 80 characters.'},{'role':'user','content':[{'type':'text','text':'Identify this item based only on what is visible. If uncertain, state uncertainty in notes.'},{'type':'image_url','image_url':{'url':image}}]}], 'response_format':{'type':'json_object'},'max_tokens':350}
            req=Request('https://api.openai.com/v1/chat/completions',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY'],'Content-Type':'application/json'})
            with urlopen(req,timeout=50) as resp: result=json.load(resp)
            answer=json.loads(result['choices'][0]['message']['content'])
            self.reply(200,{k:answer.get(k,'') for k in ['title','category','edition','confidence','notes']})
        except HTTPError as e:
            self.reply(502,{'error':'AI provider returned an error ('+str(e.code)+'). Check server configuration and billing.'})
        except Exception:
            self.reply(500,{'error':'Recognition failed. Check the server log and try again.'})
    def reply(self,code,data):
        out=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(out)));self.end_headers();self.wfile.write(out)

if __name__=='__main__':
    port=int(os.environ.get('PORT','8765'))
    print(f'FlipLedger V6 at http://localhost:{port} (AI configured: {bool(os.environ.get("OPENAI_API_KEY"))})')
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
