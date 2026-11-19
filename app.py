import json,re,time,uuid,secrets,subprocess,sys,sqlite3
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError,PhoneCodeInvalidError,PasswordHashInvalidError
R=Path(__file__).resolve().parent; D=R/"data"; A=D/"accounts"; A.mkdir(parents=True,exist_ok=True)
CF=D/"panel.json"; PW=D/"admin.txt"
if not PW.exists():
    PW.write_text("admin"); PW.chmod(0o600)
if not CF.exists(): CF.write_text('{"api_id":"","api_hash":""}')
tokens=set(); pending={}; workers={}
app=FastAPI(title="MAXO SELF")
class Login(BaseModel): password:str
class PasswordChange(BaseModel): old_password:str; new_password:str
class APIConfig(BaseModel): api_id:str; api_hash:str
class Phone(BaseModel): phone:str
class Code(BaseModel): phone:str; code:str
class TwoPass(BaseModel): phone:str; password:str
class Search(BaseModel): account:str; q:str
class SettingUpdate(BaseModel): account:str; values:dict
def auth(req):
    if req.headers.get("X-Auth") not in tokens: raise HTTPException(401,"ابتدا وارد پنل شو")
def account(a):
    if not re.fullmatch(r"[a-f0-9]{12}",a): raise HTTPException(400,"شناسه نامعتبر")
    p=A/a
    if not (p/"account.json").exists(): raise HTTPException(404,"اکانت پیدا نشد")
    return p,json.loads((p/"account.json").read_text())
def dbc(a):
    p,_=account(a); f=p/"MAXO.db"
    if not f.exists(): return None
    c=sqlite3.connect(f);c.row_factory=sqlite3.Row;return c
def start(a):
    p,c=account(a);old=workers.get(a)
    if old and old.poll() is None:return
    log=open(p/"worker.log","a",encoding="utf8")
    workers[a]=subprocess.Popen([sys.executable,str(R/"worker_runner.py"),a],cwd=R,stdout=log,stderr=subprocess.STDOUT)
def stop(a):
    p=workers.get(a)
    if p and p.poll() is None:p.terminate()
@app.get("/",response_class=HTMLResponse)
async def home():return (R/"index.html").read_text(encoding="utf8")
@app.post("/api/login")
async def login(x:Login):
    if not secrets.compare_digest(x.password,PW.read_text().strip()):raise HTTPException(401,"رمز مدیریت اشتباه است")
    t=secrets.token_urlsafe(32);tokens.add(t);return {"token":t}
@app.post("/api/change-password")
async def change_password(req:Request,x:PasswordChange):
    auth(req)
    current=PW.read_text().strip()
    if not secrets.compare_digest(x.old_password,current): raise HTTPException(400,"رمز فعلی اشتباه است")
    if len(x.new_password)<8: raise HTTPException(400,"رمز جدید باید حداقل ۸ کاراکتر باشد")
    PW.write_text(x.new_password); PW.chmod(0o600)
    return {"ok":True}
@app.post("/api/config")
async def config(req:Request,x:APIConfig):
    auth(req)
    if not x.api_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}",x.api_hash.strip()):raise HTTPException(400,"API ID یا API HASH نامعتبر است؛ HASH باید ۳۲ کاراکتر هگز باشد")
    CF.write_text(json.dumps({"api_id":int(x.api_id),"api_hash":x.api_hash.strip()}));CF.chmod(0o600);return {"ok":True}
@app.post("/api/login/send-code")
async def send(req:Request,x:Phone):
    auth(req);c=json.loads(CF.read_text())
    if not c.get("api_id") or not c.get("api_hash"):raise HTTPException(400,"ابتدا API ID و HASH را ذخیره کن")
    phone=x.phone.strip()
    if not re.fullmatch(r"\+[1-9]\d{7,14}",phone):raise HTTPException(400,"شماره باید با + و کد کشور باشد")
    old=pending.pop(phone,None)
    if old:
        try:await old["client"].disconnect()
        except:pass
    cli=TelegramClient(StringSession(),int(c["api_id"]),c["api_hash"]);await cli.connect()
    sent=await cli.send_code_request(phone);pending[phone]={"client":cli,"hash":sent.phone_code_hash,"api":c,"time":time.time()}
    return {"ok":True,"message":"کد ورود به تلگرام یا ایمیل امنیتی ارسال شد"}
@app.post("/api/login/verify-code")
async def verify(req:Request,x:Code):
    auth(req);p=pending.get(x.phone)
    if not p:raise HTTPException(400,"درخواست ورود منقضی شده؛ دوباره کد بگیر")
    try:await p["client"].sign_in(phone=x.phone,code=x.code,phone_code_hash=p["hash"])
    except SessionPasswordNeededError:return {"need_password":True}
    except PhoneCodeInvalidError:raise HTTPException(400,"کد ورود اشتباه است")
    return await finish(x.phone,p)
@app.post("/api/login/password")
async def password(req:Request,x:TwoPass):
    auth(req);p=pending.get(x.phone)
    if not p:raise HTTPException(400,"درخواست ورود منقضی شده")
    try:await p["client"].sign_in(password=x.password)
    except PasswordHashInvalidError:raise HTTPException(400,"رمز دومرحله‌ای اشتباه است")
    return await finish(x.phone,p)
async def finish(phone,p):
    me=await p["client"].get_me();sid=uuid.uuid4().hex[:12];folder=A/sid;folder.mkdir()
    obj={"id":sid,"name":" ".join(filter(None,[me.first_name,me.last_name])) or "Telegram account","phone":phone,"api_id":p["api"]["api_id"],"api_hash":p["api"]["api_hash"],"session":p["client"].session.save()}
    f=folder/"account.json";f.write_text(json.dumps(obj,ensure_ascii=False));f.chmod(0o600)
    await p["client"].disconnect();pending.pop(phone,None);start(sid);return {"ok":True,"account":sid}
@app.get("/api/accounts")
async def accounts(req:Request):
    auth(req);out=[]
    for f in A.iterdir():
        if f.is_dir() and (f/"account.json").exists():
            c=json.loads((f/"account.json").read_text());w=workers.get(f.name)
            out.append({"id":f.name,"name":c["name"],"phone":c["phone"],"running":bool(w and w.poll() is None)})
    return {"accounts":out}
@app.post("/api/accounts/{a}/start")
async def start_api(req:Request,a:str):auth(req);start(a);return {"ok":True}
@app.post("/api/accounts/{a}/stop")
async def stop_api(req:Request,a:str):auth(req);account(a);stop(a);return {"ok":True}
@app.get("/api/stats")
async def stats(req:Request):
    auth(req);na=nc=nd=nr=0
    for f in A.iterdir():
        if f.is_dir() and (f/"account.json").exists():
            na+=1;w=workers.get(f.name);nr+=int(bool(w and w.poll() is None));c=dbc(f.name)
            if c:
                try:nc+=c.execute("select count(*) from chats").fetchone()[0];nd+=c.execute("select count(*) from deleted_archive where deleted_at>0").fetchone()[0]
                except:pass
                c.close()
    mb=sum(x.stat().st_size for x in D.rglob("*") if x.is_file())/1048576
    return {"accounts":na,"chats":nc,"deleted":nd,"running":nr,"data_mb":round(mb,2)}
@app.post("/api/chats/search")
async def search(req:Request,x:Search):
    auth(req);c=dbc(x.account)
    if not c:return {"rows":[]}
    q=x.q.strip();like="%"+q+"%"
    if q:rows=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(last_message,''),coalesce(last_seen,'') from chats where cast(user_id as text)=? or username like ? or name like ? order by last_seen desc limit 100",(q.lstrip("@"),like,like)).fetchall()
    else:rows=c.execute("select user_id,coalesce(username,''),coalesce(name,''),coalesce(last_message,''),coalesce(last_seen,'') from chats order by last_seen desc limit 100").fetchall()
    c.close();return {"rows":[{"user_id":r[0],"username":"@"+r[1] if r[1] else "—","name":r[2],"last_message":r[3],"last_seen":r[4]} for r in rows]}
@app.get("/api/settings")
async def get_settings(req:Request,account:str):
    auth(req);c=dbc(account)
    if not c:return {"auto_reply":"0","archive_enabled":"1","auto_text":"","auto_style":"as_typed","name_clock":"0","bio_clock":"0"}
    try: rows=c.execute("select key,value from settings").fetchall();out={r[0]:r[1] for r in rows}
    finally:c.close()
    return {k:out.get(k,v) for k,v in {"auto_reply":"0","archive_enabled":"1","auto_text":"","auto_style":"as_typed","name_clock":"0","bio_clock":"0"}.items()}
@app.post("/api/settings")
async def put_settings(req:Request,x:SettingUpdate):
    auth(req);c=dbc(x.account)
    if not c:raise HTTPException(404,"دیتابیس اکانت هنوز ساخته نشده؛ worker را اجرا کن")
    allowed={"auto_reply","archive_enabled","auto_text","auto_style","name_clock","bio_clock"}
    for k,v in x.values.items():
        if k not in allowed:continue
        if k=="auto_text" and len(str(v))>3500:raise HTTPException(400,"متن پاسخ بیش از حد طولانی است")
        if k=="auto_style" and v not in {"as_typed","bold","quote"}:raise HTTPException(400,"استایل نامعتبر")
        c.execute("insert or replace into settings(key,value) values(?,?)",(k,str(v)))
    c.commit();c.close();return {"ok":True}
@app.get("/api/deleted")
async def deleted(req:Request,account_id:str=None,account:str=None):
    auth(req);c=dbc(account or account_id)
    if not c:return {"rows":[]}
    try:rows=c.execute("select name,sender_id,text,media_type,deleted_at from deleted_archive where deleted_at>0 order by deleted_at desc limit 300").fetchall()
    except sqlite3.OperationalError:rows=[]
    c.close();return {"rows":[{"name":r[0],"sender_id":r[1],"text":r[2],"media_type":r[3],"deleted_at":time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(r[4]))} for r in rows]}

@app.on_event("startup")
async def boot_recover():
    for folder in A.iterdir():
        if folder.is_dir() and (folder/"account.json").exists():
            try:start(folder.name)
            except Exception as e:print("WORKER START ERROR",folder.name,repr(e))
@app.get("/health")
async def health(): return {"ok":True,"service":"MAXO SELF"}
