"""Reconstruct sparse source edits from one verified immutable Git blob."""

import base64
import binascii
from hashlib import sha1

from control_plane.contracts.updater_proposal import (
    UPDATER_PROPOSAL_MAX_FILE_BYTES,
    UpdaterFileEdit,
)


def materialize_updater_edit(
    change: UpdaterFileEdit, *, base_size: int, blob: dict[str, object]
) -> str:
    """Verify provider evidence, then apply original-byte coordinates without execution."""
    content = blob.get("content")
    size = blob.get("size")
    if (
        blob.get("sha") != change.base_blob_sha
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size != base_size
        or not 0 <= base_size <= UPDATER_PROPOSAL_MAX_FILE_BYTES
        or blob.get("encoding") != "base64"
        or not isinstance(content, str)
        or len(content) > 2 * UPDATER_PROPOSAL_MAX_FILE_BYTES
    ):
        raise ValueError("Updater base blob evidence is unavailable or mismatched.")
    try:
        original = base64.b64decode("".join(content.split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Updater base blob is not valid base64.") from error
    actual_sha = sha1(f"blob {len(original)}\0".encode() + original).hexdigest()
    if len(original) != base_size or actual_sha != change.base_blob_sha:
        raise ValueError("Updater base blob content does not match its immutable identity.")
    try:
        original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Updater text edits require a UTF-8 base file.") from error
    result_size = base_size + change.size_delta
    if not 0 <= result_size <= UPDATER_PROPOSAL_MAX_FILE_BYTES:
        raise ValueError("Updater edited file exceeds the materialization size limit.")
    chunks: list[bytes] = []
    cursor = 0
    for edit in change.edits:
        expected = edit.old_text.encode("utf-8")
        end = edit.start_byte + len(expected)
        if edit.start_byte > base_size or original[edit.start_byte : end] != expected:
            raise ValueError("Updater text edit does not match the original base bytes.")
        chunks.extend((original[cursor : edit.start_byte], edit.new_text.encode("utf-8")))
        cursor = end
    chunks.append(original[cursor:])
    result = b"".join(chunks)
    if len(result) != result_size:
        raise ValueError("Updater edited file does not match its planned size.")
    if result == original:
        raise ValueError("Updater text edits do not change the base content.")
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Updater text edit splits a UTF-8 character.") from error
