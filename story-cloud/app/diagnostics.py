"""Log only import phase, exception type and code locations, never credentials."""
import logging
import re
import traceback
import uuid
from pathlib import Path

IMPORT_STAGES = {
    'archive_read':'Загрузка архива',
    'archive_extract':'Проверка и распаковка архива',
    'desktop_read':'Чтение данных Telegram Desktop',
    'desktop_session':'Подготовка исходной сессии',
    'desktop_connect':'Соединение исходной сессии с Telegram',
    'desktop_check':'Проверка исходного аккаунта',
    'cloud_session':'Подготовка облачного подключения',
    'cloud_connect':'Соединение облачной сессии с Telegram',
    'cloud_login':'Подтверждение нового входа',
    'cloud_check':'Проверка новой облачной сессии',
    'save':'Сохранение подключения',
    'unknown':'Подключение аккаунта',
}


def import_diagnostic(exc, code, stage):
    stage = stage if stage in IMPORT_STAGES else 'unknown'
    diagnostic_id = uuid.uuid4().hex[:12]
    exception_type = re.sub(r'[^A-Za-z0-9_]', '', type(exc).__name__)[:80]
    # Exclude exception messages, source text, local variables, paths and payloads.
    frames = ','.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}'
                      for f in traceback.extract_tb(exc.__traceback__)[-6:])
    logging.getLogger('story_cloud.import').warning(
        'telegram_import_failed diagnostic=%s stage=%s exception=%s code=%s frames=%s',
        diagnostic_id,stage,exception_type,code,frames)
    return {'diagnostic_id':diagnostic_id,'stage':stage,'stage_label':IMPORT_STAGES[stage]}
