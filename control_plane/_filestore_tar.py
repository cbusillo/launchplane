FILESTORE_TAR_LIBRARY = r"""
import re


def filestore_tar_warning_only(status: int, errors: bytes, truncated: bool) -> bool:
    return (status == 1 and not truncated
        and re.fullmatch(rb'(?:tar: [^\r\n]+: file changed as we read it\n)+', errors) is not None)
"""
