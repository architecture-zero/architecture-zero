"""Prove the box's locks from inside the running box.

Run it in the live container (docker exec <extractor> python -I check_locks.py)
after every deploy and in CI. Each lock prints PASS or FAIL with what it saw;
the exit code is the number of failures, so a single broken lock fails the
step that ran it.
"""
import errno
import os
import socket
import sys

# What the base image itself sets - public build facts, no secret among them.
# Anything else in the environment means something was passed in.
ALLOWED_ENV = {"PATH", "HOSTNAME", "HOME", "LANG", "GPG_KEY", "PYTHON_VERSION",
               "PYTHON_SHA256", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
               "PWD", "SHLVL", "TERM", "_"}
# The only mounts the box should carry beyond the container's own plumbing.
# "/tmp" here names the box's 16 MB tmpfs as an expected mount point; nothing
# writes a temp file from it.
ALLOWED_MOUNTS = {"/", "/tmp", "/run/extractor", "/proc", "/dev", "/sys",  # nosec B108
                  "/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
ALLOWED_MOUNT_PREFIXES = ("/proc/", "/dev/", "/sys/")

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, saw: str) -> None:
    results.append((ok, name, saw))


def _no_network() -> None:
    nets = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []
    check("no network interface but loopback", nets in ([], ["lo"]), f"interfaces={nets}")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        check("no outbound connection", False, "connected to 1.1.1.1:443")
    except OSError as e:
        check("no outbound connection", True, type(e).__name__)
    try:
        socket.getaddrinfo("example.com", 443)
        check("no name resolution", False, "example.com resolved")
    except OSError as e:
        check("no name resolution", True, type(e).__name__)


def _no_secrets() -> None:
    extra = sorted(set(os.environ) - ALLOWED_ENV)
    check("no environment passed in", not extra, f"extra={extra}")


def _no_mounts() -> None:
    seen = []
    with open("/proc/self/mounts", encoding="utf-8") as f:
        for line in f:
            target = line.split()[1]
            if target in ALLOWED_MOUNTS or target.startswith(ALLOWED_MOUNT_PREFIXES):
                continue
            seen.append(target)
    check("no mount but the socket folder", not seen, f"unexpected={seen}")


def _read_only() -> None:
    # The mount's own flags: a failed write alone proves nothing for a
    # non-root user, who could not write to / on a writable filesystem either.
    root_opts = ""
    with open("/proc/self/mounts", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if parts[1] == "/":
                root_opts = parts[3]
    check("root filesystem mounted read-only", "ro" in root_opts.split(","),
          f"options={root_opts}")
    try:
        with open("/locks-probe", "w") as f:
            f.write("x")
        os.unlink("/locks-probe")
        check("root filesystem refuses a write", False, "wrote /locks-probe")
    except OSError as e:
        check("root filesystem refuses a write", e.errno in (errno.EROFS, errno.EACCES),
              errno.errorcode.get(e.errno, str(e.errno)))


def _no_native_decoders() -> None:
    import importlib.util
    import shutil
    mods = [m for m in ("brotli", "brotlicffi") if importlib.util.find_spec(m)]
    check("no brotli decoder", not mods, f"found={mods}")
    check("no jbig2dec program", shutil.which("jbig2dec") is None,
          f"which={shutil.which('jbig2dec')}")


def _not_root() -> None:
    check("not root", os.getuid() != 0 and os.geteuid() != 0, f"uid={os.getuid()}")


def _no_capabilities() -> None:
    status = {}
    with open("/proc/self/status", encoding="utf-8") as f:
        for line in f:
            k, _, v = line.partition(":")
            status[k] = v.strip()
    caps = {k: status.get(k) for k in ("CapEff", "CapPrm", "CapBnd")}
    check("no capabilities", all(v is not None and int(v, 16) == 0 for v in caps.values()),
          f"{caps}")
    check("no new privileges", status.get("NoNewPrivs") == "1",
          f"NoNewPrivs={status.get('NoNewPrivs')}")


def _capped() -> None:
    def read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None
    mem = read("/sys/fs/cgroup/memory.max") or read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    check("memory capped", mem not in (None, "max") and int(mem) < 2**40, f"memory.max={mem}")
    pids = read("/sys/fs/cgroup/pids.max") or read("/sys/fs/cgroup/pids/pids.max")
    check("process count capped", pids not in (None, "max"), f"pids.max={pids}")


def main() -> int:
    for probe in (_no_network, _no_secrets, _no_mounts, _read_only, _not_root,
                  _no_capabilities, _capped, _no_native_decoders):
        try:
            probe()
        except Exception as e:      # a probe that cannot run is a failure, not a pass
            check(probe.__name__, False, f"probe error {type(e).__name__}: {e}")
    for ok, name, saw in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({saw})")
    failures = sum(1 for ok, _, _ in results if not ok)
    print(f"{len(results) - failures}/{len(results)} locks hold")
    return failures


if __name__ == "__main__":
    sys.exit(main())
