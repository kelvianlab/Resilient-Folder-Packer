#!/usr/bin/env python3
"""End-to-end tests for rfpack.

    python tests/test_rfpack.py                      # test rfpack.py
    python tests/test_rfpack.py --exe dist/rfpack.exe  # test a built exe

--expect-path-limit makes the long-path test first prove that the machine
really enforces Windows' 260-character limit (CI turns long-path support
off on purpose), so a pass means rfpack works around the limit rather than
the limit simply being absent.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import rfpack  # noqa: E402

WINDOWS = os.name == "nt"
RESULTS = []


def L(path):
    return rfpack.winlong(path)


# --------------------------------------------------------------------------- #

class Runner:
    def __init__(self, exe):
        self.cmd = [exe] if exe else [sys.executable, os.path.join(REPO, "rfpack.py")]

    def __call__(self, *args):
        proc = subprocess.run(self.cmd + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        out = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, out


def check(cond, msg, out=""):
    if not cond:
        raise AssertionError(msg + ("\n----- output -----\n" + out if out else ""))


def write(path, data):
    os.makedirs(os.path.dirname(L(path)), exist_ok=True)
    with open(L(path), "wb") as fh:
        fh.write(data)


def snapshot(root):
    """{relative path: sha256 or '<dir>'} for everything under root."""
    result = {}
    base = L(root)
    for dirpath, dirnames, filenames in os.walk(base):
        rel = dirpath[len(base):].strip("\\/").replace("\\", "/")
        result[rel + "/"] = "<dir>"
        for name in filenames:
            with open(os.path.join(dirpath, name), "rb") as fh:
                result[(rel + "/" if rel else "") + name] = hashlib.sha256(fh.read()).hexdigest()
    return result


def same_tree(a, b, out=""):
    sa, sb = snapshot(a), snapshot(b)
    missing = sorted(set(sa) - set(sb))
    extra = sorted(set(sb) - set(sa))
    differ = sorted(k for k in set(sa) & set(sb) if sa[k] != sb[k])
    check(not (missing or extra or differ),
          "trees differ: missing=%s extra=%s differ=%s" % (missing[:5], extra[:5], differ[:5]), out)


def make_tree(root):
    rnd = random.Random(7)
    write(os.path.join(root, "notes.txt"), b"hello\n" * 2000)
    write(os.path.join(root, "empty.bin"), b"")
    write(os.path.join(root, "sub dir", "with spaces & ampersand.txt"), b"x" * 5000)
    write(os.path.join(root, "sub dir", "deeper", "random.bin"),
          bytes(rnd.getrandbits(8) for _ in range(3 * 1024 * 1024)))
    write(os.path.join(root, "unicode", "résumé ñ 日本語.txt"), "données".encode("utf-8"))
    os.makedirs(L(os.path.join(root, "empty folder")))
    ro = os.path.join(root, "read-only.xlsx")
    write(ro, b"read only content")
    os.chmod(L(ro), stat.S_IREAD)


def manifest_of(folder):
    name = [n for n in os.listdir(folder) if n.endswith(".rfpack.json")][0]
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_roundtrip_every_codec(run, tmp, opts):
    src = os.path.join(tmp, "Project Files")
    make_tree(src)
    codecs = ["zstd", "xz", "gz", "raw"] if opts.exe else list(rfpack.available_codecs())
    for codec in codecs:
        out = os.path.join(tmp, "out-" + codec)
        dest = os.path.join(tmp, "dest-" + codec)
        code, text = run("pack", src, "-o", out, "--codec", codec, "--chunk-mb", "1")
        check(code == 0, "pack with %s failed" % codec, text)
        check(len(manifest_of(out)["parts"]) >= 2, "expected several chunks with %s" % codec, text)
        code, text = run("verify", out)
        check(code == 0, "verify with %s failed" % codec, text)
        code, text = run("unpack", out, "--into", dest)
        check(code == 0 and "Checksum matches" in text, "unpack with %s failed" % codec, text)
        same_tree(src, os.path.join(dest, "Project Files"), text)
    return "codecs: %s" % ", ".join(codecs)


def test_long_paths(run, tmp, opts):
    src = os.path.join(tmp, "Deep")
    folder = src
    for i in range(8):
        folder = os.path.join(folder, "%d. A long descriptive folder name like offices use" % i)
    target = os.path.join(folder, "PLAN-A-001 & B SITE LAYOUT REV 3.dwg")
    write(target, b"drawing" * 1000)
    write(os.path.join(src, "short.txt"), b"short")
    check(len(target) > 300, "test path is only %d characters" % len(target))

    if opts.expect_path_limit:
        try:
            with open(target, "rb"):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("this machine does not enforce the 260-character limit, "
                                 "so the test would prove nothing")

    out = os.path.join(tmp, "deep-out")
    dest = os.path.join(tmp, "deep-dest")
    code, text = run("pack", src, "-o", out, "--chunk-mb", "1")
    check(code == 0, "pack of a %d-character path failed" % len(target), text)
    code, text = run("unpack", out, "--into", dest)
    check(code == 0, "unpack of a %d-character path failed" % len(target), text)
    same_tree(src, os.path.join(dest, "Deep"), text)
    return "%d-character path, limit %s" % (
        len(target), "enforced" if opts.expect_path_limit else "not checked")


@contextlib.contextmanager
def unreadable(path):
    if WINDOWS:
        import msvcrt
        fh = open(path, "r+b")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, os.path.getsize(path))
        try:
            yield True
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, os.path.getsize(path))
            fh.close()
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            yield False
            return
        os.chmod(path, 0)
        try:
            yield True
        finally:
            os.chmod(path, 0o644)


def test_locked_file_is_skipped_not_fatal(run, tmp, opts):
    src = os.path.join(tmp, "Office")
    for i in range(30):
        write(os.path.join(src, "file%02d.txt" % i), b"data %d\n" % i * 100)
    locked = os.path.join(src, "terkunci 日本.xlsx")
    write(locked, b"someone has this open")
    out = os.path.join(tmp, "locked-out")
    dest = os.path.join(tmp, "locked-dest")
    with unreadable(locked) as active:
        if not active:
            return "SKIP (running as root, cannot make a file unreadable)"
        code, text = run("pack", src, "-o", out)
    check(code == 1, "pack should finish with warnings (exit 1), got %d" % code, text)
    check("could NOT be packed" in text, "no warning about the locked file", text)
    manifest = manifest_of(out)
    check(len(manifest["skipped"]) == 1 and "terkunci" in manifest["skipped"][0]["path"],
          "manifest does not list the locked file", text)
    check(any(n.endswith(".rfpack-log.txt") for n in os.listdir(out)), "no log file written", text)
    code, text = run("unpack", out, "--into", dest)
    check(code == 0, "unpack failed", text)
    restored = os.listdir(os.path.join(dest, "Office"))
    check(len(restored) == 30, "expected the 30 readable files, got %d" % len(restored), text)
    return "locked file skipped, 30 others restored"


def test_output_inside_source_refused(run, tmp, opts):
    src = os.path.join(tmp, "Loop")
    write(os.path.join(src, "a.txt"), b"a")
    code, text = run("pack", src, "-o", os.path.join(src, "out"))
    check(code == 2 and "inside the folder being packed" in text, "was not refused", text)


def test_overwrite_guards(run, tmp, opts):
    src = os.path.join(tmp, "Guard")
    write(os.path.join(src, "a.txt"), b"a")
    out = os.path.join(tmp, "guard-out")
    dest = os.path.join(tmp, "guard-dest")
    check(run("pack", src, "-o", out)[0] == 0, "first pack failed")
    code, text = run("pack", src, "-o", out)
    check(code == 2 and "--force" in text, "second pack was not refused", text)
    check(run("pack", src, "-o", out, "--force")[0] == 0, "--force pack failed")
    check(run("unpack", out, "--into", dest)[0] == 0, "first unpack failed")
    code, text = run("unpack", out, "--into", dest)
    check(code == 2 and "--force" in text, "second unpack was not refused", text)
    restored = os.path.join(dest, "Guard", "a.txt")
    os.chmod(L(restored), stat.S_IREAD)
    code, text = run("unpack", out, "--into", dest, "--force")
    check(code == 0, "--force unpack over a read-only file failed", text)


def test_damaged_and_missing_parts(run, tmp, opts):
    src = os.path.join(tmp, "Parts")
    make_tree(src)
    out = os.path.join(tmp, "parts-out")
    check(run("pack", src, "-o", out, "--chunk-mb", "1", "--codec", "gz")[0] == 0, "pack failed")
    parts = sorted(n for n in os.listdir(out) if ".part" in n)
    with open(os.path.join(out, parts[1]), "r+b") as fh:
        fh.seek(100)
        fh.write(b"XXXX")
    os.remove(os.path.join(out, parts[-1]))
    code, text = run("verify", out)
    check(code == 1 and parts[1] in text and parts[-1] in text and "missing" in text,
          "verify did not name both bad parts", text)
    dest = os.path.join(tmp, "parts-dest")
    code, text = run("unpack", out, "--into", dest)
    check(code == 1 and not os.path.exists(dest), "unpack did not refuse damaged parts", text)


def test_unsafe_archive_refused(run, tmp, opts):
    folder = os.path.join(tmp, "evil")
    os.makedirs(folder)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    raw = buf.getvalue()
    with open(os.path.join(folder, "evil.part001"), "wb") as fh:
        fh.write(raw)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {"tool": "resilient-folder-packer", "format": 1, "version": "test",
                "created": "x", "name": "evil", "root": "evil", "codec": "raw", "level": 0,
                "chunk_size": 1 << 20, "source_files": 1, "source_bytes": 1,
                "stream_bytes": len(raw), "stream_sha256": digest, "packed_bytes": len(raw),
                "parts": [{"name": "evil.part001", "size": len(raw), "sha256": digest}]}
    with open(os.path.join(folder, "evil.rfpack.json"), "w") as fh:
        json.dump(manifest, fh)
    dest = os.path.join(tmp, "evil-dest")
    code, text = run("unpack", folder, "--into", dest)
    check(code == 2 and "unsafe path" in text, "unsafe archive was not refused", text)
    check(not os.path.exists(os.path.join(tmp, "escaped.txt")), "file escaped the destination")


def test_dry_run_writes_nothing(run, tmp, opts):
    src = os.path.join(tmp, "Dry")
    write(os.path.join(src, "a.txt"), b"a")
    out = os.path.join(tmp, "dry-out")
    code, text = run("pack", src, "-o", out, "--dry-run")
    check(code == 0 and "Free space" in text and not os.path.exists(out), "dry run wrote", text)


def test_unc_path(run, tmp, opts):
    if not WINDOWS:
        return "SKIP (Windows only)"
    drive, rest = os.path.splitdrive(os.path.abspath(tmp))
    unc_tmp = "\\\\localhost\\%s$%s" % (drive.rstrip(":"), rest)
    if not os.path.isdir(unc_tmp):
        return "SKIP (admin share %s not reachable)" % unc_tmp
    src = os.path.join(tmp, "Share")
    make_tree(src)
    out = os.path.join(tmp, "unc-out")
    dest = os.path.join(tmp, "unc-dest")
    code, text = run("pack", os.path.join(unc_tmp, "Share"), "-o", out, "--chunk-mb", "1")
    check(code == 0, "pack from a network path failed", text)
    code, text = run("unpack", out, "--into", dest)
    check(code == 0, "unpack failed", text)
    same_tree(src, os.path.join(dest, "Share"), text)
    return "packed from %s" % os.path.join(unc_tmp, "Share")


# In-process checks that need to patch the module, so they only run against rfpack.py.

def test_space_check(run, tmp, opts):
    if opts.exe:
        return "SKIP (in-process check)"
    src = os.path.join(tmp, "Space")
    write(os.path.join(src, "a.bin"), b"a" * 100000)
    real = rfpack.shutil.disk_usage
    rfpack.shutil.disk_usage = lambda p: types.SimpleNamespace(total=0, used=0, free=1024)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rfpack.main(["pack", src, "-o", os.path.join(tmp, "space-out")])
        check(code == 2 and "not enough free space" in buf.getvalue(), "was not refused", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rfpack.main(["pack", src, "-o", os.path.join(tmp, "space-out"),
                                "--skip-space-check"])
        check(code == 0, "--skip-space-check did not proceed", buf.getvalue())
    finally:
        rfpack.shutil.disk_usage = real


def test_file_failing_mid_read(run, tmp, opts):
    if opts.exe:
        return "SKIP (in-process check)"

    class Flaky:
        def read(self, n):
            raise OSError(5, "I/O error")

    reader = rfpack.ExactReader(Flaky(), 10, b"abc")
    data = reader.read(10)
    check(data == b"abc" + b"\0" * 7 and reader.problem, "short read was not padded and flagged")
    reader = rfpack.ExactReader(io.BytesIO(b"de"), 5, b"abc")
    data = reader.read(5)
    check(data == b"abcde" and reader.problem is None, "normal read was altered")


def test_disk_full_is_a_clear_message(run, tmp, opts):
    if opts.exe:
        return "SKIP (in-process check)"
    src = os.path.join(tmp, "Full")
    write(os.path.join(src, "a.bin"), os.urandom(3 * 1024 * 1024))
    real_write = rfpack.ChunkWriter.write

    def failing_write(self, data):
        if self.total_bytes > 1024 * 1024:
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    rfpack.ChunkWriter.write = failing_write
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rfpack.main(["pack", src, "-o", os.path.join(tmp, "full-out"),
                                "--codec", "raw", "--chunk-mb", "1"])
        check(code == 2 and "filled up" in buf.getvalue(), "disk full was not reported clearly",
              buf.getvalue())
    finally:
        rfpack.ChunkWriter.write = real_write


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", help="test this built executable instead of rfpack.py")
    parser.add_argument("--expect-path-limit", action="store_true",
                        help="fail unless the OS really enforces the 260-character limit")
    opts = parser.parse_args()
    run = Runner(opts.exe)
    code, text = run("--version")
    print("Testing %s (%s)" % (" ".join(run.cmd), text.strip()))

    failures = 0
    for test in TESTS:
        tmp = tempfile.mkdtemp(prefix="rft")
        try:
            note = test(run, tmp, opts)
            print("PASS  %s%s" % (test.__name__, "  - " + note if note else ""))
        except Exception as exc:
            failures += 1
            print("FAIL  %s\n%s" % (test.__name__, exc))
        finally:
            for dirpath, dirnames, filenames in os.walk(L(tmp)):
                for name in filenames:
                    with contextlib.suppress(OSError):
                        os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
            shutil.rmtree(L(tmp), ignore_errors=True)
    print("\n%d passed, %d failed" % (len(TESTS) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
