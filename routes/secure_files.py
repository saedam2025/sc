"""Authenticated encrypted file storage shared by upload features.

New files use a chunked AES-GCM container so large uploads do not need to be
loaded into memory.  Readers also accept legacy plaintext files, allowing a
rolling migration without breaking existing attachments.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import struct
import unicodedata
import tempfile
from urllib.parse import quote
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Response, stream_with_context

from .security import load_file_secret


MAGIC = b"SAEDAMF1"
HEADER = struct.Struct(">8sQ8s")
LENGTH = struct.Struct(">I")
CHUNK_SIZE = 1024 * 1024
MAX_CIPHER_CHUNK = CHUNK_SIZE + 16
FILENAME_TOKEN_PREFIX = "~f~"


def _cipher() -> AESGCM:
    secret = load_file_secret().encode("utf-8")
    return AESGCM(hashlib.sha256(b"saedam-file-storage-v1\0" + secret).digest())


def original_filename(value: object, fallback: str = "attachment") -> str:
    name = unicodedata.normalize("NFC", str(value or "").replace("\\", "/").split("/")[-1])
    name = "".join(ch for ch in name if ch >= " " and ch not in '\x7f<>:"/\\|?*').strip(" .")
    if not name:
        name = fallback
    stem, suffix = os.path.splitext(name)
    suffix = suffix[:20]
    limit = max(1, 240 - len(suffix))
    return f"{stem[:limit]}{suffix}"


def encode_filename_token(value: object) -> str:
    """쉼표 구분 레거시 컬럼에 원본 파일명을 손실 없이 넣는다."""
    return FILENAME_TOKEN_PREFIX + quote(original_filename(value), safe="")


def decode_filename_token(value: object) -> str:
    text = str(value or "")
    if text.startswith(FILENAME_TOKEN_PREFIX):
        from urllib.parse import unquote
        return unquote(text[len(FILENAME_TOKEN_PREFIX):])
    return text


def encrypted_storage_name(source_name: object = "") -> str:
    suffix = Path(original_filename(source_name)).suffix.lower()[:20]
    return f"{secrets.token_hex(20)}{suffix}.sdf"


def _stream_size(stream: BinaryIO) -> int:
    current = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current)
    return int(size)


def encrypt_stream(stream: BinaryIO, destination: str | os.PathLike[str]) -> int:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    stream.seek(0)
    plain_size = _stream_size(stream)
    nonce_prefix = secrets.token_bytes(8)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    cipher = _cipher()
    try:
        with temporary.open("xb") as output:
            output.write(HEADER.pack(MAGIC, plain_size, nonce_prefix))
            index = 0
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                nonce = nonce_prefix + index.to_bytes(4, "big")
                encrypted = cipher.encrypt(nonce, chunk, MAGIC + index.to_bytes(4, "big"))
                output.write(LENGTH.pack(len(encrypted)))
                output.write(encrypted)
                index += 1
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return plain_size


def encrypt_upload(file_storage, destination: str | os.PathLike[str]) -> int:
    return encrypt_stream(file_storage.stream, destination)


def encrypt_bytes(data: bytes, destination: str | os.PathLike[str]) -> int:
    from io import BytesIO
    return encrypt_stream(BytesIO(data), destination)


def is_encrypted_file(path: str | os.PathLike[str]) -> bool:
    try:
        with Path(path).open("rb") as source:
            return source.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def migrate_plaintext_file(path: str | os.PathLike[str]) -> bool:
    """기존 평문 파일을 경로 변경 없이 원자적으로 암호화한다.

    여러 Gunicorn 프로세스가 동시에 같은 레거시 파일을 열어도 이중
    암호화되지 않도록 같은 디렉터리에 배타적 잠금 파일을 사용한다.
    """
    target = Path(path)
    if not target.is_file() or is_encrypted_file(target):
        return False

    lock_path = target.with_name(f".{target.name}.migration.lock")
    migration_path = target.with_name(f".{target.name}.{secrets.token_hex(8)}.migration")
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False

    try:
        os.close(descriptor)
        # 잠금을 얻기 전 다른 프로세스가 완료했을 가능성을 다시 확인한다.
        if is_encrypted_file(target):
            return False
        with target.open("rb") as source:
            encrypt_stream(source, migration_path)
        os.replace(migration_path, target)
        return True
    finally:
        migration_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def plaintext_size(path: str | os.PathLike[str]) -> int:
    target = Path(path)
    try:
        with target.open("rb") as source:
            header = source.read(HEADER.size)
        if len(header) == HEADER.size:
            magic, size, _ = HEADER.unpack(header)
            if magic == MAGIC:
                return int(size)
        return target.stat().st_size
    except OSError:
        return 0


def iter_decrypted(path: str | os.PathLike[str]) -> Iterator[bytes]:
    target = Path(path)
    source = target.open("rb")
    try:
        header = source.read(HEADER.size)
        if len(header) != HEADER.size or header[: len(MAGIC)] != MAGIC:
            source.seek(0)
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
            return

        magic, _, nonce_prefix = HEADER.unpack(header)
        cipher = _cipher()
        index = 0
        while True:
            raw_length = source.read(LENGTH.size)
            if not raw_length:
                break
            if len(raw_length) != LENGTH.size:
                raise ValueError("암호화 파일의 청크 길이가 손상되었습니다.")
            encrypted_length = LENGTH.unpack(raw_length)[0]
            if encrypted_length < 16 or encrypted_length > MAX_CIPHER_CHUNK:
                raise ValueError("암호화 파일의 청크 크기가 올바르지 않습니다.")
            encrypted = source.read(encrypted_length)
            if len(encrypted) != encrypted_length:
                raise ValueError("암호화 파일 데이터가 손상되었습니다.")
            nonce = nonce_prefix + index.to_bytes(4, "big")
            yield cipher.decrypt(nonce, encrypted, magic + index.to_bytes(4, "big"))
            index += 1
    finally:
        source.close()


def read_decrypted(path: str | os.PathLike[str], max_bytes: int | None = None) -> bytes:
    chunks = []
    total = 0
    for chunk in iter_decrypted(path):
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError("복호화 허용 크기를 초과했습니다.")
        chunks.append(chunk)
    return b"".join(chunks)


def encrypted_file_is_readable(path: str | os.PathLike[str]) -> bool:
    """응답 스트리밍 전에 현재 키로 파일의 첫 청크를 열 수 있는지 확인한다."""
    try:
        iterator = iter_decrypted(path)
        next(iterator, None)
        iterator.close()
        return True
    except (OSError, ValueError):
        return False
    except Exception as exc:
        # cryptography의 InvalidTag도 여기서 False로 바꿔 스트리밍 중 500을 막는다.
        if exc.__class__.__name__ == 'InvalidTag':
            return False
        raise


@contextmanager
def temporary_decrypted_path(path: str | os.PathLike[str], display_name: object = "attachment"):
    """파일 경로가 필요한 외부 라이브러리용 자동 삭제 평문 임시파일."""
    temporary_directory = tempfile.mkdtemp(prefix="saedam-dec-")
    temporary_name = os.path.join(temporary_directory, original_filename(display_name))
    try:
        with open(temporary_name, "xb") as output:
            for chunk in iter_decrypted(path):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        yield temporary_name
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(temporary_directory)
        except OSError:
            pass


def encrypted_response(
    path: str | os.PathLike[str],
    display_name: object,
    *,
    as_attachment: bool = True,
    mimetype: str | None = None,
) -> Response:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    # 신규 파일은 이미 암호화되어 있으며, 과거 평문 파일만 첫 접근 때
    # 같은 경로에서 전환한다. 실패 시에는 기존 호환 응답을 유지한다.
    try:
        migrate_plaintext_file(target)
    except (OSError, ValueError):
        pass
    name = original_filename(display_name)
    content_type = mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = Response(stream_with_context(iter_decrypted(target)), mimetype=content_type)
    response.content_length = plaintext_size(target)
    disposition = "attachment" if as_attachment else "inline"
    ascii_name = name.encode("ascii", "ignore").decode("ascii") or "attachment"
    ascii_name = ascii_name.replace('"', '').replace('\\', '_')
    response.headers["Content-Disposition"] = (
        f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def delete_file(path: str | os.PathLike[str] | None) -> bool:
    if not path:
        return True
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False
