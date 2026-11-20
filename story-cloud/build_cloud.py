"""Build the private mobile frontend, gateway, and encrypted R2 checkpoint endpoint."""
import json
import mimetypes
import shutil
from pathlib import Path

root=Path(__file__).parent
assets={}
for path in sorted((root/'app/static').iterdir()):
    if not path.is_file():
        continue
    key='/index.html' if path.name=='index.html' else '/assets/'+path.name
    content_type={'.js':'application/javascript','.css':'text/css','.html':'text/html',
                  '.svg':'image/svg+xml','.webmanifest':'application/manifest+json'}[path.suffix]
    assets[key]={'content':path.read_text(),'type':content_type+'; charset=utf-8'}
worker=(root/'worker/index.js').read_text().replace('__STORY_ASSETS__',json.dumps(assets,ensure_ascii=False))
dist=root/'dist'
if dist.exists():
    shutil.rmtree(dist)
(dist/'server').mkdir(parents=True)
(dist/'server/index.js').write_text(worker)
(dist/'.openai').mkdir()
shutil.copy2(root/'.openai/hosting.json',dist/'.openai/hosting.json')
print('Private gateway and frontend built.')
