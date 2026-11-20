import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .account_access import AccountAccessError

MAX_PHOTO_UPLOAD = 5 * 1024 * 1024


def prepare_avatar(data):
    if not data or len(data) > MAX_PHOTO_UPLOAD:
        raise AccountAccessError('PHOTO_SIZE', 'Выберите фото размером до 5 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ('JPEG','PNG','WEBP') or getattr(source, 'is_animated', False):
                    raise ValueError('format')
                width, height = source.size
                if min(width, height) < 160:
                    raise AccountAccessError('PHOTO_TOO_SMALL', 'Нужно фото не менее 160 × 160 пикселей.')
                if max(width, height) > 8192 or width * height > 16_000_000:
                    raise AccountAccessError('PHOTO_DIMENSIONS', 'Фото слишком большое: максимум 16 мегапикселей и 8192 пикселя по стороне.')
                source.load()
                image = ImageOps.exif_transpose(source).convert('RGBA')
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                # Square crop makes the preview match Telegram's profile presentation.
                image = ImageOps.fit(background, (min(width,height,1024),)*2, method=Image.Resampling.LANCZOS)
                photo = io.BytesIO()
                image.save(photo, 'JPEG', quality=90)
                image.thumbnail((256,256), Image.Resampling.LANCZOS)
                thumbnail = io.BytesIO()
                image.save(thumbnail, 'JPEG', quality=85)
                return photo.getvalue(), thumbnail.getvalue()
    except AccountAccessError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AccountAccessError('PHOTO_INVALID', 'Не удалось прочитать фотографию.',
                                  'Выберите обычное неподвижное изображение JPG, PNG или WebP.') from None
