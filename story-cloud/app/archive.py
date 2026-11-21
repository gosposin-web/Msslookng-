import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_UPLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 64 * 1024 * 1024
MAX_FILES = 2000


class ArchiveError(ValueError):
    pass


def extract_tdata(archive: Path, destination: Path) -> Path:
    """Extract only Desktop authentication files; never extract media caches."""
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise ArchiveError('Архив слишком большой. Уберите кэш и медиа из tdata.')
            for item in entries:
                p = PurePosixPath(item.filename.replace('\\', '/'))
                if (p.is_absolute() or '..' in p.parts or ':' in str(p)
                        or stat.S_ISLNK(item.external_attr >> 16)
                        or (item.external_attr >> 16 and not (stat.S_ISREG(item.external_attr >> 16)
                            or stat.S_ISDIR(item.external_attr >> 16)
                            or stat.S_IFMT(item.external_attr >> 16) == 0))):
                    raise ArchiveError('В архиве есть недопустимые пути или ссылки.')
                if item.flag_bits & 1:
                    raise ArchiveError('Снимите пароль с ZIP. Код-пароль tdata вводится отдельно.')
            roots = {str(PurePosixPath(e.filename.replace('\\', '/')).parent)
                     for e in entries
                     if re.fullmatch(r'key_data[s01]', PurePosixPath(e.filename.replace('\\','/')).name)}
            if len(roots) != 1:
                raise ArchiveError('Нужен один каталог tdata с файлом key_datas (или key_data0/1).')
            root = PurePosixPath(roots.pop())
            copied = 0
            for item in entries:
                p = PurePosixPath(item.filename.replace('\\', '/'))
                if item.is_dir():
                    continue
                try:
                    relative=p.relative_to(root)
                except ValueError:
                    continue
                top_level=(len(relative.parts)==1 and (
                    re.fullmatch(r'key_data[s01]',p.name)
                    or re.fullmatch(r'[A-Fa-f0-9]{16}[s01]',p.name)))
                account_map=(len(relative.parts)==2
                    and re.fullmatch(r'[A-Fa-f0-9]{16}',relative.parts[0])
                    and re.fullmatch(r'(?:map|config)[s01]',relative.parts[1]))
                if not (top_level or account_map):
                    continue
                # opentele needs the account's map before it reads the root authorization.
                # Keep only authentication/map/config files, never media caches.
                target=destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with z.open(item) as src, open(target, 'xb') as dst:
                    total = 0
                    while chunk := src.read(64 * 1024):
                        total += len(chunk)
                        copied += len(chunk)
                        if total > MAX_UNPACKED or copied > MAX_UNPACKED:
                            raise ArchiveError('Распакованный архив превышает лимит.')
                        dst.write(chunk)
                target.chmod(0o600)
            return destination
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ArchiveError('Не удалось прочитать ZIP-архив tdata.') from exc
