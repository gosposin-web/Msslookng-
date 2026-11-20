"""Account diagnostics without exposing sessions or exception payloads."""
import re
from urllib.parse import urlsplit

from telethon import functions, types


class AccountAccessError(ValueError):
    def __init__(self, code, message, hint='', *, appeal_url='', freeze_until=0, retry_after=0):
        super().__init__(message)
        self.code, self.hint = code, hint
        self.appeal_url, self.freeze_until, self.retry_after = appeal_url, freeze_until, retry_after

    def payload(self):
        result = {'detail':str(self), 'code':self.code, 'hint':self.hint}
        for key in ('appeal_url','freeze_until','retry_after'):
            if value := getattr(self, key):
                result[key] = value
        return result


def json_value(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, types.JsonObject):
        return {item.key:json_value(item.value) for item in value.value}
    if isinstance(value, types.JsonArray):
        return [json_value(item) for item in value.value]
    if isinstance(value, (types.JsonString, types.JsonNumber, types.JsonBool)):
        return value.value
    if isinstance(value, types.JsonNull):
        return None
    raise ValueError('Invalid account configuration')


def frozen_error(config=None):
    config = config or {}
    appeal = config.get('freeze_appeal_url', '')
    if not isinstance(appeal, str):
        appeal = ''
    try:
        parsed = urlsplit(appeal)
        valid_appeal = parsed.scheme == 'https' and parsed.hostname in ('telegram.org','t.me','www.telegram.org') and not parsed.username and not parsed.password
    except ValueError:
        valid_appeal = False
    if not valid_appeal:
        appeal = ''
    deadline = config.get('freeze_until_date', 0)
    if not isinstance(deadline, (int, float)) or not 0 < deadline < 32503680000:
        deadline = 0
    return AccountAccessError('ACCOUNT_FROZEN',
        'Аккаунт заморожен Telegram и работает только в режиме чтения.',
        'Добавить или редактировать его сейчас нельзя. Откройте Telegram и подайте апелляцию. Причину ограничения уточняйте в самом Telegram.',
        appeal_url=appeal, freeze_until=int(deadline))


async def inspect_account(client):
    # get_me() suppresses some authorization failures; direct RPC preserves the reason.
    try:
        users = await client(functions.users.GetUsersRequest([types.InputUserSelf()]))
        me = users[0] if users else None
    except Exception as exc:
        if type(exc).__name__ not in ('FrozenMethodInvalidError','FrozenParticipantMissingError'):
            raise
        try:
            response = await client(functions.help.GetAppConfigRequest(hash=0))
            config = json_value(response.config)
        except Exception:
            config = {}
        raise frozen_error(config) from None
    if not isinstance(me, types.User) or getattr(me, 'deleted', False):
        raise AccountAccessError('ACCOUNT_DELETED', 'Telegram не вернул действующий аккаунт.',
                                 'Проверьте вход в Telegram Desktop и создайте новый архив tdata.')
    if me.bot:
        raise AccountAccessError('ACCOUNT_UNSUPPORTED', 'Нужен пользовательский аккаунт Telegram.')
    response = await client(functions.help.GetAppConfigRequest(hash=0))
    try:
        config = json_value(response.config)
        since = config.get('freeze_since_date', 0)
        if not isinstance(since, (int, float)):
            raise ValueError('Invalid freeze status')
    except (AttributeError, TypeError, ValueError):
        raise AccountAccessError('ACCOUNT_CHECK_FAILED', 'Не удалось проверить состояние аккаунта.',
                                 'Telegram вернул неполный ответ. Повторите подключение позже.') from None
    if since > 0:
        raise frozen_error(config)
    return me


def account_error(exc):
    if isinstance(exc, AccountAccessError):
        return exc
    name = type(exc).__name__
    mapping = {
        'SessionRevokedError':('SESSION_REVOKED','Сессия аккаунта отозвана или завершена.','Войдите в Telegram Desktop заново и загрузите свежий архив tdata.'),
        'SessionExpiredError':('SESSION_EXPIRED','Срок действия сессии истёк.','Войдите в Telegram Desktop заново и создайте свежий архив tdata.'),
        'AuthKeyUnregisteredError':('SESSION_INVALID','Telegram больше не принимает ключ этой сессии.','Войдите в Telegram Desktop заново и создайте свежий архив tdata.'),
        'AuthKeyNotFound':('SESSION_INVALID','Ключ сессии не найден Telegram.','Проверьте вход в Telegram Desktop. Если ошибка повторяется, создайте новый архив.'),
        'AuthKeyDuplicatedError':('SESSION_DUPLICATED','Telegram аннулировал ключ, использованный одновременно из разных мест.','Завершите работу других программ с этим tdata, войдите заново в Desktop и создайте новый архив.'),
        'UserDeactivatedBanError':('ACCOUNT_BANNED','Аккаунт заблокирован Telegram.','Проверьте сообщение о блокировке при входе в официальный Telegram. Приложение не может снять ограничение.'),
        'PhoneNumberBannedError':('ACCOUNT_BANNED','Номер аккаунта заблокирован Telegram.','Проверьте статус номера в официальном приложении Telegram.'),
        'UserDeactivatedError':('ACCOUNT_DELETED','Аккаунт удалён или деактивирован.','Этот архив не позволяет подключить действующий аккаунт. Проверьте вход в Telegram.'),
        'SessionPasswordNeededError':('TWO_FACTOR_REQUIRED','Для входа требуется пароль двухэтапной проверки.','Введите облачный пароль Telegram в разделе «Пароли, если они установлены». Это не код из SMS.'),
        'NoPasswordProvided':('TWO_FACTOR_REQUIRED','Не указан пароль двухэтапной проверки.','Введите облачный пароль Telegram.'),
        'PasswordHashInvalidError':('TWO_FACTOR_INVALID','Пароль двухэтапной проверки не подошёл.','Проверьте облачный пароль Telegram и повторите подключение.'),
        'PasswordIncorrect':('TWO_FACTOR_INVALID','Пароль двухэтапной проверки не подошёл.','Проверьте облачный пароль Telegram.'),
        'UsernameOccupiedError':('USERNAME_OCCUPIED','Этот username уже занят.','Выберите другой username.'),
        'UsernamePurchaseAvailableError':('USERNAME_PURCHASE_REQUIRED','Этот username доступен только для покупки.','Выберите другой бесплатный username или управляйте покупкой в Telegram.'),
        'UsernameInvalidError':('USERNAME_INVALID','Telegram не принимает этот username.','Используйте 5–32 латинские буквы, цифры или знак подчёркивания. Окончательное решение принимает Telegram.'),
        'FirstNameInvalidError':('NAME_INVALID','Telegram не принимает это имя.','Укажите непустое имя длиной до 64 символов.'),
        'PhotoExtInvalidError':('PHOTO_INVALID','Telegram не принимает формат фотографии.','Выберите другое фото JPG, PNG или WebP.'),
        'PhotoCropSizeSmallError':('PHOTO_TOO_SMALL','Фотография слишком маленькая для Telegram.','Выберите изображение размером не менее 160 × 160 пикселей.'),
        'ImageProcessFailedError':('PHOTO_INVALID','Telegram не смог обработать изображение.','Выберите другую фотографию.'),
    }
    if name in mapping:
        return AccountAccessError(*mapping[name])
    if name in ('FrozenMethodInvalidError','FrozenParticipantMissingError'):
        return frozen_error()
    if name in ('ApiIdPublishedFloodError','ApiIdInvalidError'):
        return AccountAccessError('API_PARAMETERS_REJECTED','Telegram отклонил параметры приложения.',
                                  'Укажите оба своих API ID и API Hash в дополнительных настройках подключения.')
    if name in ('TDataBadDecryptKey','TDataWrongPasscode','TDataInvalidMagic','TDataReadMapDataIncorrectPasscode'):
        return AccountAccessError('TDATA_DECRYPT_FAILED','Не удалось расшифровать tdata.',
                                  'Проверьте локальный код-пароль Telegram Desktop. Архив также может быть повреждён: при необходимости создайте его заново.')
    if name.startswith(('TData','TDAccount','AccountAuthKeyNotFound')):
        return AccountAccessError('TDATA_INVALID','В архиве не найдены пригодные данные авторизации.',
                                  'Создайте ZIP с файлами авторизации действующего Telegram Desktop.')
    if name in ('FloodWaitError','FloodPremiumWaitError'):
        seconds = max(1, int(getattr(exc, 'seconds', 60)))
        return AccountAccessError('RATE_LIMIT','Telegram попросил подождать перед следующим действием.',
                                  f'Повторите через {seconds} сек. Это ограничение Telegram.', retry_after=seconds)
    if name in ('FreshTokenRequiredError','SessionTooFreshError','PasswordTooFreshError','FreshResetAuthorisationForbiddenError'):
        return AccountAccessError('SESSION_TOO_FRESH','Telegram пока запрещает это действие для новой сессии.',
                                  'Повторите позже. Срок ожидания определяется Telegram.')
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return AccountAccessError('CONNECTION_FAILED','Не удалось получить подтверждение от Telegram.',
                                  'Проверьте результат в Telegram перед повтором сохранения. При импорте можно повторить попытку позже; сбой связи не означает блокировку.')
    # Only our own validation messages are safe to display verbatim.
    if name == 'TelegramError':
        return AccountAccessError('VALIDATION_ERROR', str(exc))
    rpc = getattr(exc, 'message', '')
    code = rpc if isinstance(rpc, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,70}', rpc) else 'TELEGRAM_ERROR'
    return AccountAccessError(code,'Telegram не подтвердил действие.',
                              'Проверьте состояние аккаунта в официальном Telegram и повторите позже. Точная причина не была предоставлена.')
