"""unittest discover 실행 시 SQLite 싱글톤을 임시 폴더 삭제 전에 해제한다."""

from __future__ import annotations

import shutil
import tempfile


def _release_db_handles() -> None:
    try:
        from db_manager import reset_db_manager_cache

        reset_db_manager_cache()
    except Exception:
        pass


def _install_db_cleanup_hook() -> None:
    if getattr(tempfile.TemporaryDirectory, "_db_cleanup_installed", False):
        return

    _original_tmp_cleanup = tempfile.TemporaryDirectory.cleanup

    def cleanup(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _release_db_handles()
        return _original_tmp_cleanup(self, *args, **kwargs)

    _original_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):  # noqa: ANN002, ANN003
        _release_db_handles()
        return _original_rmtree(path, *args, **kwargs)

    tempfile.TemporaryDirectory.cleanup = cleanup  # type: ignore[method-assign]
    shutil.rmtree = rmtree
    tempfile.TemporaryDirectory._db_cleanup_installed = True


_install_db_cleanup_hook()
