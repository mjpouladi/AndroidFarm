"""Optional host-private Coolify credential, bound to one API URL."""
import json
from pathlib import Path

from installer import install
from installer.coolify_api import normalize_url
from ops.secureio import atomic_json, read_private_json, require_private_directory, require_private_file


CREDENTIAL_FILE = Path('/etc/android-farm/coolify-credential.json')
MAX_FILE_BYTES = 16384


def validate_token(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096 or
            any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError('توکن Coolify معتبر نیست؛ مقدار آن نمایش داده نمی‌شود.')
    return value


def _path(path: Path | None) -> Path:
    selected = Path(path if path is not None else CREDENTIAL_FILE).absolute()
    if install._path_traverses_symlink(selected):
        raise RuntimeError('مسیر فایل خصوصی توکن Coolify نباید symlink داشته باشد.')
    return selected


def load(url: str, *, path: Path | None = None) -> str | None:
    """Never send a remembered credential to a different API origin."""
    expected_url = normalize_url(url)
    selected = _path(path)
    if not selected.exists():
        return None
    require_private_file(selected, 'Coolify credential')
    if selected.stat().st_size > MAX_FILE_BYTES:
        raise RuntimeError('فایل خصوصی توکن Coolify بیش از حد بزرگ است.')
    value = read_private_json(selected, 'Coolify credential')
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'url', 'token'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            not isinstance(value['url'], str)):
        raise RuntimeError('ساختار فایل خصوصی توکن Coolify معتبر نیست.')
    if normalize_url(value['url']) != expected_url:
        raise RuntimeError('توکن ذخیره‌شده متعلق به آدرس Coolify دیگری است؛ '
                           'برای آدرس جدید --remember-token یا --token-file را صریح تعیین کنید.')
    return validate_token(value['token'])


def save(url: str, token: str, *, path: Path | None = None) -> Path:
    """Caller must authenticate first; atomic replacement preserves old bytes on failure."""
    payload = {'schema_version': 1, 'url': normalize_url(url), 'token': validate_token(token)}
    if len((json.dumps(payload, indent=2, sort_keys=True) + '\n').encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('اطلاعات توکن Coolify بیش از حد مجاز است.')
    selected = _path(path)
    require_private_directory(selected.parent, 'Coolify credential directory', create=True)
    if selected.exists():
        require_private_file(selected, 'Coolify credential')
    atomic_json(selected, payload)
    return selected
