"""Package the same frontend for the private preview. Production is served by FastAPI."""
import shutil
from pathlib import Path

root=Path(__file__).parent
dist=root/'dist'
dist.mkdir(exist_ok=True)
shutil.copytree(root/'app/static',dist/'assets',dirs_exist_ok=True)
shutil.copy2(root/'app/static/index.html',dist/'index.html')
