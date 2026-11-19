import json,re,time,uuid,secrets,subprocess,sys,sqlite3,shutil,os,base64,pickle,html as html_lib
from pathlib import Path
from html.parser import HTMLParser
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,FileResponse,PlainTextResponse
from pydantic import BaseModel,Field
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityBold,MessageEntityItalic,MessageEntityUnderline,MessageEntityTextUrl,MessageEntityBlockquote,MessageEntityCode
from telethon.errors import SessionPasswordNeededError,PhoneCodeInvalidError,PasswordHashInvalidError

R=Path(__file__).resolve().parent; D=R/"data"; A=D/"accounts"; A.mkdir(parents=True,exist_ok=True)
CF=D/"panel.json"; PW=D/"admin.txt"
if not PW.exists(): PW.write_text("admin",encoding="utf8"); PW.chmod(0o600)
if not CF.exists(): CF.write_text('{"api_id":"","api_hash":""}',encoding="utf8")
tokens=set(); pending={}; workers={}
app=FastAPI(title="MAXO SELF CONTROL")

class Login(BaseModel): password:str
class PasswordChange(BaseModel): old_password:str; new_password:str
class APIConfig(BaseModel): api_id:str; api_hash:str
class Phone(BaseModel): phone:str
class Code(BaseModel): phone:str; code:str
class TwoPass(BaseModel): phone:str; password:str
class Search(BaseModel): account:str; q:str=""
class SettingUpdate(BaseModel): account:str; values:dict
class RichText(BaseModel): account:str; html:str
class Action(BaseModel): command:str; payload:dict=Field(default_factory=dict)
class DeleteAccount(BaseModel): confirm:str

def auth(req):
    if req.headers.get("X-Auth") not in tokens: raise HTTPException(401,"ابتدا وارد پنل شو")
def account(a):
    if not re.fullmatch(r"[a-f0-9]{12}",a): raise HTTPException(400,"شناسه نامعتبر")
    p=A/a; f=p/"account.json"
    if not f.exists(): raise HTTPException(404,"اکانت پیدا نشد")
    return p,json.loads(f.read_text(encoding="utf8"))
def dbc(a):
    p,_=account(a); f=p/"MAXO.db"
    if not f.exists(): return None
    c=sqlite3.connect(f,timeout=20); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=20000")
    return c
def start(a):
    p,_=account(a); old=workers.get(a)
    if old and old.poll() is None:return
    log=open(p/"worker.log","a",encoding="utf8")
    workers[a]=subprocess.Popen([sys.executable,str(R/"worker_runner.py"),a],cwd=R,stdout=log,stderr=subprocess.STDOUT)
def stop(a):
    proc=workers.get(a)
    if proc and proc.poll() is None: proc.terminate()
class TelegramRichParser(HTMLParser):
    ALLOWED={"b":"bold","strong":"bold","i":"italic","em":"italic","u":"underline","blockquote":"quote","code":"code","a":"link"}
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.entities=[]; self.stack=[]; self.safe=[]
    def plain(self): return "".join(self.parts)
    def utf16_offset(self): return len(self.plain().encode("utf-16-le"))//2
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs); tag=tag.lower()
        if tag=="br": self.parts.append("\n"); self.safe.append("<br>"); return
        if tag=="blockquote" and self.parts and not self.plain().endswith("\n"):
            self.parts.append("\n"); self.safe.append("<br>")
        if tag in ("div","p") and self.parts and not self.plain().endswith("\n"):
            self.parts.append("\n"); self.safe.append("<br>")
        kind=self.ALLOWED.get(tag)
        if not kind: return
        url=attrs.get("href","") if kind=="link" else ""
        if kind=="link" and not re.match(r"^https?://",url,re.I): kind=None
        if not kind: return
        self.stack.append((tag,kind,self.utf16_offset(),url))
        if kind=="link": self.safe.append('<a href="'+html_lib.escape(url,quote=True)+'" rel="noopener">')
        else: self.safe.append("<"+("strong" if kind=="bold" else "em" if kind=="italic" else tag)+">")
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag in ("div","p"):
            if self.parts and not self.plain().endswith("\n"): self.parts.append("\n"); self.safe.append("<br>")
        for i in range(len(self.stack)-1,-1,-1):
            t,kind,start,url=self.stack[i]
            if t==tag:
                self.stack.pop(i); length=self.utf16_offset()-start
                if length>0:
                    if kind=="bold": e=MessageEntityBold(offset=start,length=length)
                    elif kind=="italic": e=MessageEntityItalic(offset=start,length=length)
                    elif kind=="underline": e=MessageEntityUnderline(offset=start,length=length)
                    elif kind=="quote":
                        try: e=MessageEntityBlockquote(offset=start,length=length,collapsed=False)
                        except TypeError: e=MessageEntityBlockquote(offset=start,length=length)
                    elif kind=="code": e=MessageEntityCode(offset=start,length=length)
                    else: e=MessageEntityTextUrl(offset=start,length=length,url=url)
                    self.entities.append(e)
                self.safe.append("</a>" if kind=="link" else "</strong>" if kind=="bold" else "</em>" if kind=="italic" else "</"+tag+">")
                if tag=="blockquote" and not self.plain().endswith("\n"):
                    self.parts.append("\n"); self.safe.append("<br>")
                break
    def handle_data(self,data): self.parts.append(data); self.safe.append(html_lib.escape(data))


def ensure_queue(c):
    c.execute("""CREATE TABLE IF NOT EXISTS web_commands(id INTEGER PRIMARY KEY AUTOINCREMENT,command TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',result TEXT DEFAULT '',created_at INTEGER,finished_at INTEGER)""")
    c.commit()
def enqueue(a,command,payload):
    c=dbc(a)
    if c is None: raise HTTPException(409,"دیتابیس اکانت آماده نیست؛ چند ثانیه صبر کن")
    ensure_queue(c); cur=c.execute("INSERT INTO web_commands(command,payload,status,created_at) VALUES(?,?,'queued',?)",(command,json.dumps(payload,ensure_ascii=False),int(time.time())))
    cid=cur.lastrowid;c.commit();c.close();return cid

@app.get("/",response_class=HTMLResponse)
async def home(): return (R/"index.html").read_text(encoding="utf8")
@app.post("/api/login")
async def login(x:Login):
    if not secrets.compare_digest(x.password,PW.read_text(encoding="utf8").strip()): raise HTTPException(401,"رمز مدیریت اشتباه است")
    t=secrets.token_urlsafe(32);tokens.add(t);return {"token":t}
@app.post("/api/change-password")
async def change_password(req:Request,x:PasswordChange):
    auth(req); current=PW.read_text(encoding="utf8").strip()
    if not secrets.compare_digest(x.old_password,current): raise HTTPException(400,"رمز فعلی اشتباه است")
    if len(x.new_password)<8: raise HTTPException(400,"رمز جدید باید حداقل ۸ کاراکتر باشد")
    PW.write_text(x.new_password,encoding="utf8");PW.chmod(0o600);tokens.clear();return {"ok":True}
@app.get("/api/config")
async def get_config(req:Request):
    auth(req);c=json.loads(CF.read_text(encoding="utf8"));return {"api_id":str(c.get("api_id","")),"api_hash_saved":bool(c.get("api_hash"))}
@app.post("/api/config")
async def config(req:Request,x:APIConfig):
    auth(req)
    if not x.api_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}",x.api_hash.strip()): raise HTTPException(400,"API ID یا API HASH نامعتبر است؛ HASH باید ۳۲ کاراکتر هگز باشد")
    CF.write_text(json.dumps({"api_id":int(x.api_id),"api_hash":x.api_hash.strip()}),encoding="utf8");CF.chmod(0o600);return {"ok":True}
@app.post("/api/login/send-code")
async def send(req:Request,x:Phone):
    auth(req);c=json.loads(CF.read_text(encoding="utf8"))
    if not c.get("api_id") or not c.get("api_hash"): raise HTTPException(400,"ابتدا API ID و HASH را ذخیره کن")
    phone=x.phone.strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}",phone): raise HTTPException(400,"شماره باید با + و کد کشور باشد")
    old=pending.pop(phone,None)
    if old:
        try: await old["client"].disconnect()
        except: pass
    cli=TelegramClient(StringSession(),int(c["api_id"]),c["api_hash"]);await cli.connect()
    sent=await cli.send_code_request(phone);pending[phone]={"client":cli,"hash":sent.phone_code_hash,"api":c,"time":time.time()}
    return {"ok":True,"message":"کد ورود به تلگرام یا مسیر تأیید اعلام‌شده از طرف تلگرام ارسال شد"}
@app.post("/api/login/verify-code")
async def verify(req:Request,x:Code):
    auth(req);p=pending.get(x.phone)
    if not p: raise HTTPException(400,"درخواست ورود منقضی شده؛ دوباره کد بگیر")
    try: await p["client"].sign_in(phone=x.phone,code=x.code,phone_code_hash=p["hash"])
    except SessionPasswordNeededError: return {"need_password":True}
    except PhoneCodeInvalidError: raise HTTPException(400,"کد ورود اشتباه است")
    return await finish(x.phone,p)
@app.post("/api/login/password")
async def password(req:Request,x:TwoPass):
    auth(req);p=pending.get(x.phone)
    if not p: raise HTTPException(400,"درخواست ورود منقضی شده")
    try: await p["client"].sign_in(password=x.password)
    except PasswordHashInvalidError: raise HTTPException(400,"رمز دومرحله‌ای اشتباه است")
    return await finish(x.phone,p)
async def finish(phone,p):
    me=await p["client"].get_me();sid=uuid.uuid4().hex[:12];folder=A/sid;folder.mkdir(parents=True)
    obj={"id":sid,"name":" ".join(filter(None,[me.first_name,me.last_name])) or "Telegram account","phone":phone,"api_id":p["api"]["api_id"],"api_hash":p["api"]["api_hash"],"session":p["client"].session.save(),"telegram_id":me.id,"username":me.username or ""}
    f=folder/"account.json";f.write_text(json.dumps(obj,ensure_ascii=False),encoding="utf8");f.chmod(0o600)
    await p["client"].disconnect();pending.pop(phone,None);start(sid);return {"ok":True,"account":sid}
@app.get("/api/accounts")
async def accounts(req:Request):
    auth(req);out=[]
    for f in A.iterdir():
        if f.is_dir() and (f/"account.json").exists():
            c=json.loads((f/"account.json").read_text(encoding="utf8"));w=workers.get(f.name)
            out.append({"id":f.name,"name":c.get("name","Telegram"),"phone":c.get("phone",""),"username":c.get("username",""),"telegram_id":c.get("telegram_id",""),"running":bool(w and w.poll() is None)})
    return {"accounts":out}
@app.post("/api/accounts/{a}/start")
async def start_api(req:Request,a:str): auth(req);start(a);return {"ok":True}
@app.post("/api/accounts/{a}/stop")
async def stop_api(req:Request,a:str): auth(req);account(a);stop(a);return {"ok":True}
@app.delete("/api/accounts/{a}")
async def remove_account(req:Request,a:str):
    auth(req);p,_=account(a);stop(a);shutil.rmtree(p,ignore_errors=True);workers.pop(a,None);return {"ok":True}
@app.get("/api/stats")
async def stats(req:Request):
    auth(req);na=nc=nd=nr=nb=nm=0;total_replies=0
    for f in A.iterdir():
        if f.is_dir() and (f/"account.json").exists():
            na+=1;w=workers.get(f.name);nr+=int(bool(w and w.poll() is None));c=dbc(f.name)
            if c:
                try:
                    nc+=c.execute("select count(*) from chats").fetchone()[0];nd+=c.execute("select count(*) from deleted_archive where deleted_at>0").fetchone()[0];nb+=c.execute("select count(*) from blocked").fetchone()[0];nm+=c.execute("select count(*) from muted").fetchone()[0];total_replies+=c.execute("select coalesce(sum(replies),0) from chats").fetchone()[0]
                except sqlite3.Error: pass
                c.close()
    mb=sum(x.stat().st_size for x in D.rglob("*") if x.is_file())/1048576
    return {"accounts":na,"chats":nc,"deleted":nd,"running":nr,"blocked":nb,"muted":nm,"replies":total_replies,"data_mb":round(mb,2)}
@app.get("/api/account-info")
async def account_info_api(req:Request,account:str):
    auth(req);p,cfg=globals()["account"](account);out={"name":cfg.get("name"),"phone":cfg.get("phone"),"username":cfg.get("username"),"telegram_id":cfg.get("telegram_id")}
    con=dbc(account)
    if con:
        for key,sql in [("chats","select count(*) from chats"),("replies","select coalesce(sum(replies),0) from chats"),("blocked","select count(*) from blocked"),("muted","select count(*) from muted")]:
            try:out[key]=con.execute(sql).fetchone()[0]
            except:out[key]=0
        con.close()
    return out
@app.post("/api/chats/search")
async def search(req:Request,x:Search):
    auth(req);c=dbc(x.account)
    if not c:return {"rows":[]}
    q=x.q.strip();like="%"+q+"%"
    if q: rows=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(last_message,''),coalesce(last_seen,''),coalesce(received,0),coalesce(replies,0) from chats where cast(user_id as text)=? or username like ? or name like ? order by last_seen desc limit 200",(q.lstrip("@"),like,like)).fetchall()
    else: rows=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(last_message,''),coalesce(last_seen,''),coalesce(received,0),coalesce(replies,0) from chats order by last_seen desc limit 200").fetchall()
    c.close();return {"rows":[{"user_id":r[0],"username":"@"+r[1] if r[1] else "—","name":r[2],"last_message":r[3],"last_seen":r[4],"received":r[5],"replies":r[6]} for r in rows]}
@app.get("/api/settings")
async def get_settings(req:Request,account:str):
    auth(req);c=dbc(account)
    defaults={"auto_reply":"0","archive_enabled":"1","auto_text":"سلام، فعلاً آفلاین هستم. بعداً پیام شما را می‌بینم.","auto_html":"سلام، فعلاً آفلاین هستم. بعداً پیام شما را می‌بینم.","auto_style":"as_typed","name_clock":"0","name_font":"1","name_label":"MAXO","bio_clock":"0","bio_font":"1","chat_font_enabled":"0","chat_font_style":"normal"}
    if not c:return defaults
    try: rows=c.execute("select key,value from settings").fetchall();out={r[0]:r[1] for r in rows}
    finally:c.close()
    result={k:out.get(k,v) for k,v in defaults.items()}
    if "auto_html" not in out: result["auto_html"]=html_lib.escape(result["auto_text"]).replace("\n","<br>")
    return result
@app.post("/api/settings")
async def put_settings(req:Request,x:SettingUpdate):
    auth(req);c=dbc(x.account)
    if not c:raise HTTPException(404,"دیتابیس اکانت هنوز ساخته نشده؛ worker را اجرا کن")
    allowed={"auto_reply","archive_enabled","auto_text","auto_style","name_clock","name_font","name_label","bio_clock","bio_font","chat_font_enabled","chat_font_style"}
    if "auto_text" in x.values:
        c.execute("DELETE FROM settings WHERE key IN ('auto_text_entities','auto_html')")
    for k,v in x.values.items():
        if k not in allowed:continue
        if k=="auto_text" and len(str(v))>3500:raise HTTPException(400,"متن پاسخ بیش از حد طولانی است")
        if k=="auto_style" and v not in {"as_typed","bold","quote"}:raise HTTPException(400,"استایل پاسخ خودکار نامعتبر")
        if k=="chat_font_style" and v not in {"normal","keshide","bold","italic","bold_keshide"}:raise HTTPException(400,"فونت پیام نامعتبر")
        c.execute("insert or replace into settings(key,value) values(?,?)",(k,str(v)))
    c.commit();c.close();return {"ok":True}
@app.post("/api/auto-rich")
async def save_auto_rich(req:Request,x:RichText):
    auth(req); c=dbc(x.account)
    if c is None: raise HTTPException(404,"دیتابیس اکانت آماده نیست")
    if len(x.html)>20000: raise HTTPException(400,"محتوای متن بیش از حد طولانی است")
    parser=TelegramRichParser(); parser.feed(x.html); parser.close()
    plain=parser.plain()
    if not plain.strip(): raise HTTPException(400,"متن پاسخ خالی است")
    if len(plain)>3500: raise HTTPException(400,"متن پاسخ بیش از حد طولانی است")
    blob=base64.b64encode(pickle.dumps(parser.entities)).decode("ascii")
    safe_html="".join(parser.safe).strip()
    c.execute("insert or replace into settings(key,value) values('auto_text',?)",(plain,))
    c.execute("insert or replace into settings(key,value) values('auto_text_entities',?)",(blob,))
    c.execute("insert or replace into settings(key,value) values('auto_html',?)",(safe_html,))
    c.commit();c.close();return {"ok":True,"length":len(plain)}
@app.get("/api/lists")
async def lists(req:Request,account:str):
    auth(req);c=dbc(account)
    if not c:return {"blocked":[],"muted":[]}
    try:
        b=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(created_at,'') from blocked order by created_at desc").fetchall()
        m=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(created_at,'') from muted order by created_at desc").fetchall()
    finally:c.close()
    def pack(rows):return [{"user_id":r[0],"username":("@"+r[1]) if r[1] else "—","name":r[2],"created_at":r[3]} for r in rows]
    return {"blocked":pack(b),"muted":pack(m)}
@app.get("/api/deleted")
async def deleted(req:Request,account:str):
    auth(req);c=dbc(account)
    if not c:return {"rows":[]}
    try:rows=c.execute("select msg_id,chat_id,name,sender_id,username,text,media_type,media_path,created_at,deleted_at from deleted_archive where deleted_at>0 order by deleted_at desc limit 500").fetchall()
    except sqlite3.OperationalError:rows=[]
    c.close()
    return {"rows":[{"msg_id":r[0],"chat_id":r[1],"name":r[2],"sender_id":r[3],"username":r[4],"text":r[5],"media_type":r[6],"has_media":bool(r[7]),"created_at":time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(r[8] or 0)),"deleted_at":time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(r[9] or 0))} for r in rows]}
@app.get("/api/deleted-media/{a}/{msg_id}")
async def deleted_media(req:Request,a:str,msg_id:int):
    auth(req);c=dbc(a)
    if not c:raise HTTPException(404,"فایل پیدا نشد")
    row=c.execute("select media_path from deleted_archive where msg_id=? and deleted_at>0",(msg_id,)).fetchone();c.close()
    if not row or not row[0]:raise HTTPException(404,"رسانه ذخیره نشده است")
    path=Path(row[0]).resolve();base=(A/a).resolve()
    if base not in path.parents or not path.is_file():raise HTTPException(404,"فایل در دسترس نیست")
    return FileResponse(path,filename=path.name)
@app.post("/api/action/{a}")
async def action(req:Request,a:str,x:Action):
    auth(req);account(a)
    allowed={"block","unblock","mute","unmute","delete_messages","delete_message","spam","name_clock","name_clock_off","bio_clock","bio_clock_off","export_start","export_stop","status","account_info"}
    if x.command not in allowed:raise HTTPException(400,"عملیات پشتیبانی نمی‌شود")
    cid=enqueue(a,x.command,x.payload);return {"command_id":cid}
@app.get("/api/action/{a}/{cid}")
async def action_status(req:Request,a:str,cid:int):
    auth(req);c=dbc(a)
    if not c:return {"status":"missing"}
    ensure_queue(c);r=c.execute("select status,result from web_commands where id=?",(cid,)).fetchone();c.close()
    return {"status":r[0],"result":r[1]} if r else {"status":"missing"}
@app.get("/api/export-status")
async def export_status(req:Request,account:str):
    auth(req);c=dbc(account)
    if not c:return {"running":False}
    try:
        rows=c.execute("select key,value from settings where key like 'web_export_%'").fetchall();d={r[0]:r[1] for r in rows}
    except: d={}
    c.close();return {"running":d.get("web_export_running","0")=="1","target":d.get("web_export_target",""),"forwarded":int(d.get("web_export_forwarded","0")),"uploaded":int(d.get("web_export_uploaded","0")),"failed":int(d.get("web_export_failed","0")),"message":d.get("web_export_message","")}
@app.get("/api/logs")
async def logs(req:Request,account:str,lines:int=100):
    auth(req);p,_=globals()["account"](account);f=p/"worker.log"
    if not f.exists():return {"text":"هنوز لاگی ثبت نشده است"}
    data=f.read_text(encoding="utf8",errors="replace").splitlines()[-max(10,min(lines,500)):]
    return {"text":"\n".join(data)}
@app.on_event("startup")
async def boot_recover():
    for folder in A.iterdir():
        if folder.is_dir() and (folder/"account.json").exists():
            try:start(folder.name)
            except Exception as e:print("WORKER START ERROR",folder.name,repr(e))
@app.get("/health")
async def health():return {"ok":True,"service":"MAXO SELF"}
