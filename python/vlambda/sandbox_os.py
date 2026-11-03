"""Landlock + seccomp self-confinement for runner processes.

The runner runs under the same container as the server, so this module builds
an unprivileged OS-level sandbox before any user code is executed:

* Landlock limits filesystem access to an explicit allow-list (read-only
  Python/venv/project paths plus one writable temporary directory).
* seccomp blocks network, signals to other processes, ptrace, mount,
  namespace changes and other dangerous syscalls.

No user namespaces, capabilities, compose flags or host sysctl changes are
required. Kernel >= 5.13 with Landlock is needed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import resource

# Landlock access rights (ABI v1-v5).
LL_EXECUTE = 1 << 0
LL_WRITE_FILE = 1 << 1
LL_READ_FILE = 1 << 2
LL_READ_DIR = 1 << 3
LL_REMOVE_DIR = 1 << 4
LL_REMOVE_FILE = 1 << 5
LL_MAKE_CHAR = 1 << 6
LL_MAKE_DIR = 1 << 7
LL_MAKE_REG = 1 << 8
LL_MAKE_SOCK = 1 << 9
LL_MAKE_FIFO = 1 << 10
LL_MAKE_BLOCK = 1 << 11
LL_MAKE_SYM = 1 << 12
LL_REFER = 1 << 13
LL_TRUNCATE = 1 << 14
LL_IOCTL_DEV = 1 << 15

_LL_ABI1 = (1 << 13) - 1
_LL_ABI2 = _LL_ABI1 | LL_REFER
_LL_ABI3 = _LL_ABI2 | LL_TRUNCATE
_LL_ABI5 = _LL_ABI3 | LL_IOCTL_DEV

_READ_FILE_ACCESS = LL_READ_FILE | LL_EXECUTE
_READ_DIR_ACCESS = _READ_FILE_ACCESS | LL_READ_DIR
_WRITE_DIR_ACCESS = (
    LL_READ_FILE | LL_WRITE_FILE | LL_READ_DIR | LL_REMOVE_DIR | LL_REMOVE_FILE
    | LL_MAKE_DIR | LL_MAKE_REG | LL_MAKE_SOCK | LL_MAKE_FIFO | LL_MAKE_SYM
    | LL_REFER | LL_TRUNCATE
)
_DEVICE_ACCESS = LL_READ_FILE | LL_WRITE_FILE

_RULE_PATH_BENEATH = 1
_CREATE_RULESET_VERSION = 1

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_MASKED_EQ = 7

# Syscalls that must never be reachable from user code. Most are blocked with
# EPERM/EACCES so Python can report a normal exception instead of dying.
_DENY_SYSCALLS = (
    # network
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "sendto", "recvfrom", "sendmsg", "recvmsg", "sendmmsg", "recvmmsg",
    "shutdown", "getsockname", "getpeername", "setsockopt", "getsockopt",
    # process creation / execution
    "fork", "vfork", "execve", "execveat",
    # process control / signals
    "kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo",
    "pidfd_open", "pidfd_send_signal", "pidfd_getfd", "process_vm_readv",
    "process_vm_writev", "process_madvise", "process_mrelease", "ptrace",
    "kcmp", "prlimit64", "sched_setaffinity", "sched_setattr",
    "sched_setscheduler", "setpriority", "ioprio_set", "migrate_pages",
    "move_pages",
    # filesystem / namespaces / mounts
    "mount", "umount", "umount2", "pivot_root", "chroot", "setns", "unshare",
    "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick",
    "mount_setattr", "open_by_handle_at", "name_to_handle_at",
    # kernel attack surface
    "bpf", "perf_event_open", "keyctl", "add_key", "request_key",
    "kexec_load", "kexec_file_load", "reboot", "swapon", "swapoff",
    "init_module", "finit_module", "delete_module", "acct", "quotactl",
    "syslog", "adjtimex", "clock_settime", "settimeofday", "sethostname",
    "setdomainname", "vhangup", "nfsservctl", "userfaultfd",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    # privilege changes (the runner is already unprivileged)
    "setuid", "setgid", "setreuid", "setregid", "setresuid", "setresgid",
    "setgroups", "capset",
)


def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    lib.prctl.restype = ctypes.c_int
    lib.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    return lib


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def _syscalls() -> tuple[int, int, int]:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "aarch64", "arm64"):
        return 444, 445, 446
    raise RuntimeError(f"Landlock is not supported on {machine}")


def _landlock_abi(libc) -> int:
    nr, _, _ = _syscalls()
    ret = libc.syscall(
        ctypes.c_long(nr), ctypes.c_void_p(0), ctypes.c_size_t(0),
        ctypes.c_uint(_CREATE_RULESET_VERSION),
    )
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock_create_ruleset: {os.strerror(err)}")
    return int(ret)


def _handled_mask(abi: int) -> int:
    if abi >= 5:
        return _LL_ABI5
    if abi >= 3:
        return _LL_ABI3
    if abi >= 2:
        return _LL_ABI2
    return _LL_ABI1


def _add_rule(libc, ruleset_fd: int, path: str, access: int):
    try:
        pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    try:
        attr = _PathBeneath(access, pfd)
        _, add_nr, _ = _syscalls()
        ret = libc.syscall(
            ctypes.c_long(add_nr), ctypes.c_int(ruleset_fd),
            ctypes.c_uint(_RULE_PATH_BENEATH), ctypes.byref(attr), ctypes.c_uint(0),
        )
        if ret != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_add_rule({path}): {os.strerror(err)}")
    finally:
        os.close(pfd)


def _apply_landlock(read_paths: list[str], rw_paths: list[str]):
    libc = _libc()
    abi = _landlock_abi(libc)
    handled = _handled_mask(abi)
    attr = ctypes.c_uint64(handled)
    create_nr, _, restrict_nr = _syscalls()
    ruleset_fd = libc.syscall(
        ctypes.c_long(create_nr), ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint(0),
    )
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock_create_ruleset: {os.strerror(err)}")
    try:
        for path in read_paths:
            if not path or not os.path.exists(path):
                continue
            access = _READ_DIR_ACCESS if os.path.isdir(path) else _READ_FILE_ACCESS
            _add_rule(libc, ruleset_fd, path, access & handled)
        for path in rw_paths:
            if not path or not os.path.exists(path):
                continue
            if os.path.isdir(path):
                access = _WRITE_DIR_ACCESS
            else:
                access = _DEVICE_ACCESS
            _add_rule(libc, ruleset_fd, path, access & handled)
        # Devices Python needs for normal operation.
        for path in ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random"):
            if os.path.exists(path):
                _add_rule(libc, ruleset_fd, path, _DEVICE_ACCESS & handled)
        # Landlock requires no_new_privs when the caller is not privileged.
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            err = ctypes.get_errno()
            raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")
        ret = libc.syscall(ctypes.c_long(restrict_nr), ctypes.c_int(ruleset_fd), ctypes.c_uint(0))
        if ret != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_restrict_self: {os.strerror(err)}")
    finally:
        os.close(ruleset_fd)


def _load_seccomp():
    last_error = None
    for soname in ("libseccomp.so.2", "libseccomp.so"):
        try:
            lib = ctypes.CDLL(soname)
            break
        except OSError as exc:
            last_error = exc
    else:
        raise RuntimeError(f"libseccomp not available: {last_error}")

    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(_ScmpArgCmp),
    ]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    ctx = lib.seccomp_init(_SCMP_ACT_ALLOW)
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        action = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _DENY_SYSCALLS:
            nr = lib.seccomp_syscall_resolve_name(name.encode())
            if nr < 0:
                continue
            rc = lib.seccomp_rule_add_array(ctx, action, nr, 0, None)
            if rc != 0:
                raise RuntimeError(f"seccomp rule failed for {name}: {rc}")
        # glibc falls back to clone() when clone3() reports ENOSYS.
        clone3_nr = lib.seccomp_syscall_resolve_name(b"clone3")
        if clone3_nr >= 0:
            rc = lib.seccomp_rule_add_array(
                ctx, _SCMP_ACT_ERRNO | errno.ENOSYS, clone3_nr, 0, None,
            )
            if rc != 0:
                raise RuntimeError(f"seccomp clone3 rule failed: {rc}")
        # Threads are allowed (CLONE_THREAD); fork-like clone calls are denied.
        clone_nr = lib.seccomp_syscall_resolve_name(b"clone")
        if clone_nr >= 0:
            clone_thread = 0x00010000
            cmp = _ScmpArgCmp(0, _SCMP_CMP_MASKED_EQ, clone_thread, 0)
            rc = lib.seccomp_rule_add_array(ctx, action, clone_nr, 1, ctypes.byref(cmp))
            if rc != 0:
                raise RuntimeError(f"seccomp clone rule failed: {rc}")
        rc = lib.seccomp_load(ctx)
        if rc != 0:
            raise RuntimeError(f"seccomp_load failed: {rc}")
    finally:
        lib.seccomp_release(ctx)


def _apply_rlimits(cpu_sec: int = 5):
    limits = (
        (resource.RLIMIT_AS, 2048 * 1024 * 1024),
        (resource.RLIMIT_CPU, max(1, cpu_sec)),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_FSIZE, 16 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    )
    for kind, value in limits:
        try:
            resource.setrlimit(kind, (value, value))
        except (OSError, ValueError):
            pass


def apply_sandbox(read_paths: list[str], rw_paths: list[str], cpu_sec: int = 5):
    """Confine the current process. Must run before user code."""
    _apply_rlimits(cpu_sec)
    _apply_landlock(read_paths, rw_paths)
    _load_seccomp()


def sandbox_supported() -> bool:
    try:
        return _landlock_abi(_libc()) >= 1
    except Exception:
        return False
