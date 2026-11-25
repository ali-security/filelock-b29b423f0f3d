from __future__ import annotations

import os
import sys
from contextlib import suppress
from errno import EACCES, ENOSYS
from pathlib import Path
from stat import S_ISVTX
from typing import cast

from ._api import BaseFileLock

#: a flag to indicate if the fcntl API is available
has_fcntl = False
if sys.platform == "win32":  # pragma: win32 cover

    class UnixFileLock(BaseFileLock):
        """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""

        def _acquire(self) -> None:
            raise NotImplementedError

        def _release(self) -> None:
            raise NotImplementedError

else:  # pragma: win32 no cover
    try:
        import fcntl
    except ImportError:
        pass
    else:
        has_fcntl = True

    class UnixFileLock(BaseFileLock):
        """Uses the :func:`fcntl.flock` to hard lock the lock file on unix systems."""

        def _acquire(self) -> None:
            opened = self._open_lock_file()
            if opened is None:
                return  # unlinked since the create attempt; let the retry loop start over
            fd, created = opened
            if created:
                # Before the flock: a peer may win it first, and only the creator may set the mode. A file that was
                # already at the lock path, such as one renamed or hard-linked there, keeps its permissions.
                try:
                    with suppress(PermissionError):  # This locked is not owned by this UID
                        os.fchmod(fd, self._context.mode)
                except BaseException:
                    os.close(fd)
                    raise
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exception:
                os.close(fd)
                if exception.errno == ENOSYS:  # NotImplemented error
                    msg = "FileSystem does not appear to support flock; user SoftFileLock instead"
                    raise NotImplementedError(msg) from exception
            else:
                try:
                    self._refuse_foreign_owner(fd)
                except BaseException:
                    os.close(fd)
                    raise
                self._context.lock_file_fd = fd

        def _refuse_foreign_owner(self, fd: int) -> None:
            # The owner of a file under a sticky parent may unlink it during the hold. A contender then locks a new
            # inode at the path, and both hold the lock.
            owner = _foreign_owner(os.fstat(fd), Path(self.lock_file).parent.stat())
            if owner is not None:
                reason = f"lock file belongs to uid {owner}, not to this process's account or the directory's owner"
                raise PermissionError(EACCES, reason, self.lock_file)

        def _open_lock_file(self) -> tuple[int, bool] | None:
            # No O_TRUNC: a contender that loses the flock must not truncate the holder's file, and neither may the
            # winner, since a file renamed or hard-linked to the lock path is indistinguishable from a lock file.
            # O_EXCL tells whether this attempt created the inode: only then does it get the explicit mode.
            open_flags = os.O_RDWR
            o_nofollow = getattr(os, "O_NOFOLLOW", None)
            if o_nofollow is not None:
                open_flags |= o_nofollow
            try:
                return os.open(self.lock_file, open_flags | os.O_CREAT | os.O_EXCL, self._context.mode), True
            except FileExistsError:
                pass
            try:
                return os.open(self.lock_file, open_flags), False
            except FileNotFoundError:
                return None  # unlinked since the create attempt; let the retry loop start over

        def _release(self) -> None:
            # Do not remove the lockfile:
            #   https://github.com/tox-dev/py-filelock/issues/31
            #   https://stackoverflow.com/questions/17708885/flock-removing-locked-file-without-race-condition
            fd = cast(int, self._context.lock_file_fd)
            self._context.lock_file_fd = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _foreign_owner(node: os.stat_result, parent: os.stat_result) -> int | None:
        # Return the uid that owns *node* when another account controls it under a sticky parent.
        #
        # A lock relies on its file staying in place. The sticky bit (a directory flag that lets only a file's owner
        # remove it) on a parent such as ``/tmp`` stops other accounts from renaming or removing the node. One that
        # this account or the parent's owner owns is therefore safe there.
        #
        # One that another account owns is outside the lock's control: its owner can remove it while the lock is held.
        # Without the sticky bit, any process that can create the node can also remove this account's own one, so a
        # refusal there would change nothing.
        #
        # Linux applies the same rule for ``fs.protected_regular``.
        if parent.st_mode & S_ISVTX:
            return None if node.st_uid in {os.geteuid(), parent.st_uid} else node.st_uid
        return None


__all__ = [
    "has_fcntl",
    "UnixFileLock",
]
