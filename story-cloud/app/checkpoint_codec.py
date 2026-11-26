"""Bounded SQLite page patches; every frame is encrypted by SnapshotStore."""
import hashlib
import struct

MAX_DATABASE = 32 * 1024 * 1024
MAX_WIRE = 16 * 1024 * 1024
MAX_PATCHES = 256
BUNDLE_MAGIC = b'ZSC2\n'
PATCH_HEADER = struct.Struct('>4sIII32s32s')
MAX_PATCH = MAX_DATABASE + MAX_DATABASE // 512 * 4 + PATCH_HEADER.size


def page_size(data):
    if len(data) < 100 or not data.startswith(b'SQLite format 3\x00'):
        raise ValueError('Not a SQLite database')
    size = int.from_bytes(data[16:18], 'big')
    size = 65536 if size == 1 else size
    if size < 512 or size > 65536 or size & (size - 1) or len(data) % size:
        raise ValueError('Invalid SQLite page size')
    if len(data) > MAX_DATABASE:
        raise ValueError('Database too large')
    return size


def encode_patch(previous, current):
    size = page_size(current)
    if page_size(previous) != size:
        return None
    changed = []
    for offset in range(0, len(current), size):
        page = current[offset:offset + size]
        if previous[offset:offset + size] != page:
            changed.append(struct.pack('>I', offset // size) + page)
    return PATCH_HEADER.pack(b'ZSP1', len(current), size, len(changed),
                             hashlib.sha256(previous).digest(),
                             hashlib.sha256(current).digest()) + b''.join(changed)


def apply_patch(previous, patch):
    if not PATCH_HEADER.size <= len(patch) <= MAX_PATCH:
        raise ValueError('Invalid patch length')
    magic, length, size, count, before, after = PATCH_HEADER.unpack_from(patch)
    if magic != b'ZSP1' or size != page_size(previous):
        raise ValueError('Invalid patch format')
    if length < size or length > MAX_DATABASE or length % size or count > length // size:
        raise ValueError('Invalid patch bounds')
    if len(patch) != PATCH_HEADER.size + count * (4 + size):
        raise ValueError('Truncated patch')
    if hashlib.sha256(previous).digest() != before:
        raise ValueError('Wrong patch base')
    result = bytearray(previous[:length])
    result.extend(b'\0' * (length - len(result)))
    offset, last = PATCH_HEADER.size, -1
    for _ in range(count):
        index = struct.unpack_from('>I', patch, offset)[0]
        offset += 4
        if not last < index < length // size:
            raise ValueError('Invalid patch page')
        result[index * size:(index + 1) * size] = patch[offset:offset + size]
        offset += size
        last = index
    result = bytes(result)
    if page_size(result) != size or hashlib.sha256(result).digest() != after:
        raise ValueError('Patch integrity failed')
    return result


def unpack_frames(data):
    if not data or len(data) > MAX_WIRE:
        raise ValueError('Invalid checkpoint size')
    if not data.startswith(BUNDLE_MAGIC):
        return [data]
    frames, offset = [], len(BUNDLE_MAGIC)
    while offset < len(data):
        if offset + 4 > len(data) or len(frames) > MAX_PATCHES:
            raise ValueError('Invalid checkpoint frames')
        length = struct.unpack_from('>I', data, offset)[0]
        offset += 4
        if not length or offset + length > len(data):
            raise ValueError('Truncated checkpoint frame')
        frames.append(data[offset:offset + length])
        offset += length
    if not frames:
        raise ValueError('Missing checkpoint base')
    return frames
