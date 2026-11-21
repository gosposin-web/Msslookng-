"""Read bounded TXT/CSV chat lists without fetching URLs or retaining uploads."""
import csv
import io
import re
from dataclasses import dataclass

from .telegram import TelegramError, normalize_chat

MAX_CHAT_FILE = 512 * 1024
MAX_FILE_CHATS = 200
MAX_FILE_ROWS = 2000
HEADERS = {'url','link','chat','chaturl','chatlink','chatid','username','telegram',
           'ссылка','чат','ссылканачат','ссылканагруппу','юзернейм','идчата'}


class ChatFileError(ValueError):
    pass


@dataclass
class ChatList:
    refs: list[str]
    duplicates: int
    invalid: list[dict]
    invalid_count: int


def ref_key(ref):
    # Invite tokens are case-sensitive; public usernames are not.
    return ref.casefold() if ref.startswith('@') else ref


def parse_chat_file(data, filename):
    suffix = filename.rsplit('.',1)[-1].lower() if '.' in filename else ''
    if suffix not in ('txt','csv'):
        raise ChatFileError('Нужен файл TXT или CSV со списком чатов.')
    if not data or len(data)>MAX_CHAT_FILE:
        raise ChatFileError('Выберите непустой файл размером до 512 КБ.')
    try:
        if data.startswith((b'\xff\xfe',b'\xfe\xff')):
            text = data.decode('utf-16')
        else:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('cp1251')
    except UnicodeError:
        raise ChatFileError('Не удалось прочитать текст. Сохраните файл в UTF-8.') from None
    if '\x00' in text or any(ord(c)<32 and c not in '\t\n\r' for c in text):
        raise ChatFileError('Файл содержит двоичные данные. Нужен обычный текст TXT или CSV.')
    lines = text.splitlines()
    if len(lines)>MAX_FILE_ROWS:
        raise ChatFileError('В файле слишком много строк. Оставьте до 200 уникальных чатов.')
    delimiter = None
    if suffix=='csv':
        sample='\n'.join(line for line in lines if line.strip() and not line.lstrip().startswith('#'))[:8192]
        try:
            delimiter=csv.Sniffer().sniff(sample,delimiters=',;\t').delimiter
        except csv.Error:
            # A single-column CSV is valid too. Prefer the first visible separator.
            first=sample.splitlines()[0] if sample else ''
            delimiter=next((d for d in (';','\t',',') if d in first),',')
    reader=csv.reader(io.StringIO(text),delimiter=delimiter,strict=True) if delimiter else None
    rows=reader if reader else ([line] for line in lines)
    refs=[];seen=set();duplicates=0;invalid=[];invalid_count=0;columns=None;first_row=True
    try:
        for number,row in enumerate(rows,1):
            line=reader.line_num if reader else number
            if not row or not any(c.strip() for c in row) or row[0].lstrip().startswith('#'):
                continue
            if first_row:
                first_row=False
                found=[i for i,c in enumerate(row) if re.sub(r'[\s_\-]','',c.casefold().strip()) in HEADERS]
                has_reference=any(c.strip().startswith(('@','+','-','http://','https://','t.me/','telegram.me/','joinchat/')) for c in row)
                if found and not has_reference:
                    columns=found
                    continue
            if columns is not None:
                cells=[row[i].strip() for i in columns if i<len(row) and row[i].strip()]
            else:
                cells=[c.strip() for c in row if c.strip()]
                explicit=[c for c in cells if c.startswith(('@','+','-','https://','http://','t.me/','telegram.me/','joinchat/'))]
                if len(cells)>1:
                    cells=explicit
            if not cells:
                cells=['']
            row_seen=set()
            for cell in cells:
                try:
                    if len(cell)>200:
                        raise TelegramError('Слишком длинная ссылка или username.')
                    ref=normalize_chat(cell)
                except (TelegramError,ValueError):
                    invalid_count+=1
                    if len(invalid)<20:
                        invalid.append({'line':line,'value':cell[:120],
                                        'message':'Нужна ссылка t.me, @username или отрицательный ID группы.'})
                    continue
                key=ref_key(ref)
                if key in row_seen:
                    continue
                row_seen.add(key)
                if key in seen:
                    duplicates+=1
                    continue
                seen.add(key);refs.append(ref)
                if len(refs)>MAX_FILE_CHATS:
                    raise ChatFileError('В одном файле допускается до 200 уникальных чатов. Разделите список на несколько файлов.')
    except csv.Error:
        raise ChatFileError('Не удалось разобрать CSV. Проверьте кавычки и разделитель или загрузите TXT: один чат на строку.') from None
    if not refs:
        raise ChatFileError('В файле не найдено ни одного корректного чата. Используйте t.me/имя, @username или ID группы.')
    return ChatList(refs,duplicates,invalid,invalid_count)
