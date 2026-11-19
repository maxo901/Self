import ast
from pathlib import Path
for name in ("app.py","worker_runner.py","core_bot.py"):
    ast.parse(Path(name).read_text(encoding="utf-8"),filename=name)
    print(name,"syntax OK")
