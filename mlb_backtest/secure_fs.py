"""Capability-based trusted directory roots for security-sensitive path operations."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RootBinding:
    """The lexical path and directory identities that define a trusted root."""

    path: Path
    component_names: tuple[str, ...]
    component_identities: tuple[tuple[int, int], ...]


_CONSTRUCTOR_TOKEN = object()


class TrustedRoot:
    """An owned descriptor chain from ``/`` to one lexical directory path."""

    def __init__(
        self,
        binding: RootBinding,
        descriptors: tuple[int, ...],
        token: object,
    ) -> None:
        if token is not _CONSTRUCTOR_TOKEN:
            raise RuntimeError("TrustedRoot instances must be opened by open_trusted_root")
        self._binding = binding
        self._descriptors = descriptors

    @property
    def path(self) -> Path:
        return self._binding.path

    @property
    def binding(self) -> RootBinding:
        return self._binding

    @property
    def root_fd(self) -> int:
        self._require_open()
        return self._descriptors[-1]

    def assert_bound(self) -> None:
        """Reject a changed component name or descriptor identity."""
        self._require_open()
        try:
            for index, descriptor in enumerate(self._descriptors):
                descriptor_stat = os.fstat(descriptor)
                actual = _identity(descriptor_stat)
                expected = self._binding.component_identities[index]
                if actual != expected or not stat.S_ISDIR(descriptor_stat.st_mode):
                    raise ValueError("unsafe trusted root binding")
                if index:
                    named = os.stat(
                        self._binding.component_names[index],
                        dir_fd=self._descriptors[index - 1],
                        follow_symlinks=False,
                    )
                    if (
                        _identity(named) != actual
                        or not stat.S_ISDIR(named.st_mode)
                    ):
                        raise ValueError("unsafe trusted root binding")
        except OSError as exc:
            raise ValueError("unsafe trusted root binding") from exc

    def dup_root_fd(self) -> int:
        self._require_open()
        try:
            return os.dup(self._descriptors[-1])
        except OSError as exc:
            raise ValueError("unsafe trusted root descriptor") from exc

    def close(self) -> None:
        """Release the complete descriptor chain. Repeated calls are harmless."""
        descriptors, self._descriptors = self._descriptors, ()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "TrustedRoot":
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    def _require_open(self) -> None:
        if not self._descriptors:
            raise RuntimeError("trusted root capability is closed")


def _secure_fs_test_hook(stage: str, component: str | None = None) -> None:
    """No-op seam for deterministic filesystem race tests."""


def open_trusted_root(
    path: Path, *, expected: RootBinding | None = None
) -> TrustedRoot:
    """Open and bind a directory chain without resolving any symlinks."""
    flags = _directory_open_flags()
    lexical_path = Path(os.path.abspath(path))
    component_names = ("/", *lexical_path.parts[1:])
    descriptors: list[int] = []
    trusted: TrustedRoot | None = None
    try:
        root_descriptor = os.open("/", flags)
        descriptors.append(root_descriptor)
        root_stat = os.fstat(root_descriptor)
        _require_directory(root_stat)
        identities = [_identity(root_stat)]
        _secure_fs_test_hook("after_component_open", "/")

        for component in component_names[1:]:
            parent_descriptor = descriptors[-1]
            descriptor = os.open(component, flags, dir_fd=parent_descriptor)
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_directory(opened)
            _require_directory(named)
            if _identity(opened) != _identity(named):
                raise ValueError("unsafe trusted root binding")
            identities.append(_identity(opened))
            _secure_fs_test_hook("after_component_open", component)

        binding = RootBinding(
            path=lexical_path,
            component_names=component_names,
            component_identities=tuple(identities),
        )
        trusted = TrustedRoot(binding, tuple(descriptors), _CONSTRUCTOR_TOKEN)
        descriptors = []
        _secure_fs_test_hook("before_return_revalidation")
        trusted.assert_bound()
        if expected is not None and binding != expected:
            raise ValueError("trusted root binding differs from expected")
        return trusted
    except (OSError, ValueError) as exc:
        if trusted is not None:
            trusted.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, ValueError):
            raise
        raise ValueError("unsafe trusted root") from exc
    except BaseException:
        if trusted is not None:
            trusted.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _directory_open_flags() -> int:
    required_names = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    required_flags = tuple(getattr(os, name, 0) for name in required_names)
    if not all(required_flags):
        raise ValueError("trusted root filesystem support is unavailable")
    return os.O_RDONLY | required_flags[0] | required_flags[1] | required_flags[2]


def _identity(result: os.stat_result) -> tuple[int, int]:
    return (result.st_dev, result.st_ino)


def _require_directory(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("unsafe trusted root binding")
