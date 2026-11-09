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
_SCMP_CMP_NE = 1
_SCMP_CMP_EQ = 4
_SCMP_CMP_MASKED_EQ = 7

# fcntl commands that redirect SIGIO/SIGURG to another process.
_F_SETOWN = 8
_F_SETSIG = 10
_F_SETOWN_EX = 15
# Safe fcntl commands whose numeric value has bit 3 set, so the masked allow
# rule below would otherwise hide them.
_F_GETPIPE_SZ = 1032
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_GET_RW_HINT = 1035
_F_SET_RW_HINT = 1036
_F_GET_FILE_RW_HINT = 1037
_F_SET_FILE_RW_HINT = 1038

# Allow-list for the default seccomp mode. It covers CPython startup, SQLite,
# SSL, threads, asyncio, the Unix-socket broker client and normal file I/O.
# Network syscalls that could create AF_INET sockets, process creation,
# kernel attack surface and privilege changes are absent on purpose: the
# default action denies them. clone/socket/socketpair get conditional rules
# below because Python needs threads and AF_UNIX sockets.
_ALLOW_SYSCALLS = (
    "read", "write", "readv", "writev", "pread64", "pwrite64",
    "exit", "exit_group",
    "preadv", "pwritev", "preadv2", "pwritev2", "lseek", "llseek",
    "close", "close_range", "dup", "dup2", "dup3", "ioctl", "flock",
    "fstat", "newfstatat", "statx", "stat", "lstat", "statfs", "fstatfs",
    "access", "faccessat", "faccessat2", "readlink", "readlinkat",
    "getcwd", "chdir", "fchdir", "getdents", "getdents64",
    "open", "openat", "openat2", "creat",
    "unlink", "unlinkat", "rename", "renameat", "renameat2",
    "mkdir", "mkdirat", "rmdir", "link", "linkat", "symlink", "symlinkat",
    "chmod", "fchmod", "fchmodat", "fchmodat2",
    "chown", "fchown", "lchown", "fchownat", "umask",
    "utime", "utimes", "utimensat", "futimesat",
    "truncate", "ftruncate", "fallocate",
    "fsync", "fdatasync", "sync", "syncfs", "sync_file_range",
    "sendfile", "copy_file_range", "splice", "tee", "readahead",
    "mmap", "munmap", "mprotect", "mremap", "msync", "madvise", "brk",
    "mlock", "mlock2", "munlock", "mlockall", "munlockall", "memfd_create",
    "getrandom", "membarrier", "rseq", "futex", "futex_waitv",
    "getpid", "getppid", "gettid", "getuid", "geteuid", "getgid", "getegid",
    "getgroups", "getresuid", "getresgid", "getpgrp", "getpgid", "getsid",
    "set_tid_address", "set_robust_list", "get_robust_list", "getcpu",
    "sched_yield", "sched_getaffinity", "sched_getscheduler",
    "sched_getparam", "sched_get_priority_max", "sched_get_priority_min",
    "getpriority",
    "nanosleep", "clock_nanosleep", "clock_gettime", "clock_getres",
    "gettimeofday", "time", "getitimer", "setitimer", "alarm",
    "timer_create", "timer_settime", "timer_gettime", "timer_getoverrun",
    "timer_delete", "timerfd_create", "timerfd_settime", "timerfd_gettime",
    "restart_syscall",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "rt_sigpending",
    "rt_sigtimedwait", "rt_sigsuspend", "sigaltstack",
    "epoll_create", "epoll_create1", "epoll_ctl", "epoll_wait",
    "epoll_pwait", "epoll_pwait2", "poll", "ppoll", "select", "pselect6",
    "eventfd", "eventfd2", "pipe", "pipe2", "signalfd4",
    "connect", "getsockname", "getpeername", "getsockopt", "setsockopt",
    "shutdown", "sendto", "recvfrom",
    "prctl", "arch_prctl", "uname", "sysinfo",
    "getrlimit", "setrlimit", "getrusage", "times",
    "fadvise64", "fadvise64_64",
    "inotify_init", "inotify_init1", "inotify_add_watch", "inotify_rm_watch",
)

# Syscalls that must never be reachable from user code. Most are blocked with
# EPERM/EACCES so Python can report a normal exception instead of dying.
_DENY_SYSCALLS = (
    # network: only client connections to AF_UNIX sockets are allowed. The
    # socket() rule is added separately in _load_seccomp so AF_INET/AF_INET6
    # remain blocked while the capability broker stays reachable.
    "bind", "listen", "accept", "accept4",
    "sendmsg", "recvmsg", "sendmmsg", "recvmmsg",
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


_seccomp_handle = None


def _seccomp_lib():
    """Load libseccomp once, before Landlock hides the library path."""
    global _seccomp_handle
    if _seccomp_handle is not None:
        return _seccomp_handle
    last_error = None
    for soname in ("libseccomp.so.2", "libseccomp.so"):
        try:
            _seccomp_handle = ctypes.CDLL(soname)
            return _seccomp_handle
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"libseccomp not available: {last_error}")


def _load_seccomp():
    lib = _seccomp_lib()

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

    def resolve(name: str) -> int:
        return lib.seccomp_syscall_resolve_name(name.encode())

    def add(ctx, action: int, name: str, comparisons=None):
        nr = resolve(name)
        if nr < 0:
            return
        if comparisons:
            array = (_ScmpArgCmp * len(comparisons))(*comparisons)
            rc = lib.seccomp_rule_add_array(ctx, action, nr, len(comparisons), array)
        else:
            rc = lib.seccomp_rule_add_array(ctx, action, nr, 0, None)
        if rc != 0:
            raise RuntimeError(f"seccomp rule failed for {name}: {rc}")

    mode = os.environ.get("VLAMBDA_SECCOMP_MODE", "allowlist").lower()
    if mode not in ("allowlist", "denylist"):
        mode = "allowlist"
    default_action = _SCMP_ACT_ERRNO | errno.EPERM
    ctx = lib.seccomp_init(_SCMP_ACT_ALLOW if mode == "denylist" else default_action)
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        allow = _SCMP_ACT_ALLOW
        deny = default_action
        if mode == "denylist":
            for name in _DENY_SYSCALLS:
                add(ctx, deny, name)
            # Only AF_UNIX client sockets are allowed, for the capability broker.
            add(ctx, deny, "socket", [_ScmpArgCmp(0, _SCMP_CMP_NE, 1, 0)])
            add(ctx, deny, "socketpair", [_ScmpArgCmp(0, _SCMP_CMP_NE, 1, 0)])
            # Threads are allowed (CLONE_THREAD); fork-like clone calls are denied.
            add(ctx, deny, "clone", [_ScmpArgCmp(0, _SCMP_CMP_MASKED_EQ, 0x00010000, 0)])
            # Signal redirection to another process through fcntl.
            for cmd in (_F_SETOWN, _F_SETSIG, _F_SETOWN_EX):
                add(ctx, deny, "fcntl", [_ScmpArgCmp(1, _SCMP_CMP_EQ, cmd, 0)])
        else:
            for name in _ALLOW_SYSCALLS:
                add(ctx, allow, name)
            # AF_UNIX only: the capability broker is a Unix socket. No other
            # socket family can be created, so egress has no direct socket.
            add(ctx, allow, "socket", [_ScmpArgCmp(0, _SCMP_CMP_EQ, 1, 0)])
            add(ctx, allow, "socketpair", [_ScmpArgCmp(0, _SCMP_CMP_EQ, 1, 0)])
            # Threads only; fork/vfork/clone-without-CLONE_THREAD stay denied.
            add(ctx, allow, "clone", [_ScmpArgCmp(0, _SCMP_CMP_MASKED_EQ, 0x00010000, 0x00010000)])
            # Only the calling process may adjust its limits; otherwise user
            # code could change another tenant's process limits.
            add(ctx, allow, "prlimit64", [_ScmpArgCmp(0, _SCMP_CMP_EQ, 0, 0)])
            # fcntl is allowed for descriptor flags and file locks. Commands
            # 8-15 (F_SETOWN/F_GETOWN/F_SETSIG/F_SETOWN_EX) can deliver signals
            # to other same-uid processes, so they are left to the default deny.
            add(ctx, allow, "fcntl", [_ScmpArgCmp(1, _SCMP_CMP_MASKED_EQ, 0x8, 0)])
            for cmd in (
                _F_GETPIPE_SZ, _F_ADD_SEALS, _F_GET_SEALS,
                _F_GET_RW_HINT, _F_SET_RW_HINT,
                _F_GET_FILE_RW_HINT, _F_SET_FILE_RW_HINT,
            ):
                add(ctx, allow, "fcntl", [_ScmpArgCmp(1, _SCMP_CMP_EQ, cmd, 0)])
        # glibc falls back to clone() when clone3() reports ENOSYS.
        add(ctx, _SCMP_ACT_ERRNO | errno.ENOSYS, "clone3")
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
    # Landlock hides most of the filesystem, including the directory that holds
    # libseccomp. Load it first, then restrict the filesystem, then install the
    # syscall filter.
    _seccomp_lib()
    _apply_landlock(read_paths, rw_paths)
    _load_seccomp()


def sandbox_supported() -> bool:
    try:
        return _landlock_abi(_libc()) >= 1
    except Exception:
        return False
