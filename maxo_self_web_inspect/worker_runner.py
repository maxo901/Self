import os,sys,json,runpy
from pathlib import Path
R=Path(__file__).resolve().parent
p=R/"data"/"accounts"/sys.argv[1]
c=json.loads((p/"account.json").read_text())
os.environ.update(API_ID=str(c["api_id"]),API_HASH=c["api_hash"],SESSION_STRING=c["session"],DATA_DIR=str(p))
os.chdir(p)
runpy.run_path(str(R/"core_bot.py"),run_name="__main__")
