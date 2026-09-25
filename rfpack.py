#!/usr/bin/env python3
"""Resilient Folder Packer - pack a folder into verifiable chunks, move them over
any flaky link, and rebuild without redoing the whole transfer.

Single file, no installer, standard library only. Uses Zstandard when the
optional `zstandard` package is present, and falls back to stdlib compression
otherwise so the same file still runs on a machine you just plugged into.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time

__version__ = "1.0.0"

MANIFEST_SUFFIX = ".rfpack.json"
DEFAULT_CHUNK_MB = 64
DEFAULT_LEVEL = 6
READ_BLOCK = 1024 * 1024


# --------------------------------------------------------------------------- #
# Compression backends
# --------------------------------------------------------------------------- #

def available_codecs():
    codecs = {}
    try:
        import zstandard  # noqa: F401
        codecs["zstd"] = "zstandard"
    except ImportError:
        pass
    codecs["xz"] = "lzma (stdlib)"
    codecs["gz"] = "zlib (stdlib)"
    codecs["raw"] = "no compression"
    return codecs


def pick_codec(requested):
    codecs = available_codecs()
    if requested == "auto":
        return "zstd" if "zstd" in codecs else "gz"
    if requested not in codecs:
        raise Usage(
            "codec %r is not available here (have: %s). Install it with "
            "`pip install zstandard`, or pass --codec gz to stay on the "
            "standard library." % (requested, ", ".join(sorted(codecs)))
        )
    return requested


def compressor(codec, level):
    """Return (write_target_factory, ) style object exposing compress/flush."""
    if codec == "zstd":
        import zstandard
        ctx = zstandard.ZstdCompressor(level=level, threads=-1)
        return ctx.compressobj()
    if codec == "xz":
        import lzma
        return lzma.LZMACompressor(preset=min(level, 9))
    if codec == "gz":
        import zlib
        return zlib.compressobj(min(level, 9), zlib.DEFLATED, zlib.MAX_WBITS | 16)
    return _NullCodec()


def decompressor(codec):
    if codec == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj()
    if codec == "xz":
        import lzma
        return lzma.LZMADecompressor()
    if codec == "gz":
        import zlib
        return zlib.decompressobj(zlib.MAX_WBITS | 16)
    return _NullCodec()


class _NullCodec:
    def compress(self, data):
        return data

    def decompress(self, data):
        return data

    def flush(self):
        return b""


# --------------------------------------------------------------------------- #
# Chunked output
# --------------------------------------------------------------------------- #

class Usage(Exception):
    """A problem the user can fix from the command line."""


class ChunkWriter(io.RawIOBase):
    """Writes a byte stream into fixed-size part files, hashing as it goes."""

    def __init__(self, out_dir, base_name, chunk_size):
        self.out_dir = out_dir
        self.base_name = base_name
        self.chunk_size = chunk_size
        self.parts = []
        self._fh = None
        self._written = 0
        self._hash = None
        self.total_bytes = 0

    def writable(self):
        return True

    def _open_next(self):
        index = len(self.parts) + 1
        name = "%s.part%03d" % (self.base_name, index)
        self._fh = open(os.path.join(self.out_dir, name), "wb")
        self._hash = hashlib.sha256()
        self._written = 0
        self._name = name

    def _close_current(self):
        if self._fh is None:
            return
        self._fh.close()
        self.parts.append({
            "name": self._name,
            "size": self._written,
            "sha256": self._hash.hexdigest(),
        })
        self._fh = None

    def write(self, data):
        data = bytes(data)
        view = memoryview(data)
        while view:
            if self._fh is None:
                self._open_next()
            room = self.chunk_size - self._written
            piece = view[:room]
            self._fh.write(piece)
            self._hash.update(piece)
            self._written += len(piece)
            self.total_bytes += len(piece)
            view = view[room:]
            if self._written >= self.chunk_size:
                self._close_current()
        return len(data)

    def close(self):
        self._close_current()


class CompressingWriter(io.RawIOBase):
    def __init__(self, sink, codec, level):
        self.sink = sink
        self.codec = compressor(codec, level)
        self.raw_bytes = 0
        self.hash = hashlib.sha256()

    def writable(self):
        return True

    def write(self, data):
        data = bytes(data)
        self.raw_bytes += len(data)
        self.hash.update(data)
        blob = self.codec.compress(data)
        if blob:
            self.sink.write(blob)
        return len(data)

    def close(self):
        tail = self.codec.flush()
        if tail:
            self.sink.write(tail)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def human(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return "%.1f %s" % (num, unit) if unit != "B" else "%d B" % num
        num /= 1024.0


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_source(src):
    files = 0
    total = 0
    for root, _dirs, names in os.walk(src):
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            files += 1
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return files, total


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("tool") != "resilient-folder-packer":
        raise Usage("%s is not a Resilient Folder Packer manifest." % path)
    return data


def find_manifest(target):
    if os.path.isfile(target) and target.endswith(MANIFEST_SUFFIX):
        return target
    if os.path.isdir(target):
        hits = sorted(n for n in os.listdir(target) if n.endswith(MANIFEST_SUFFIX))
        if not hits:
            raise Usage("no %s manifest found in %s" % (MANIFEST_SUFFIX, target))
        if len(hits) > 1:
            raise Usage(
                "several archives found in %s (%s) - point at one manifest file "
                "instead of the folder." % (target, ", ".join(hits))
            )
        return os.path.join(target, hits[0])
    raise Usage("cannot find a manifest at %s" % target)


def check_parts(manifest_path, manifest, deep=True, progress=True):
    """Return (ok_parts, problems) where problems is a list of (name, reason)."""
    folder = os.path.dirname(os.path.abspath(manifest_path))
    ok, problems = [], []
    for i, part in enumerate(manifest["parts"], 1):
        path = os.path.join(folder, part["name"])
        if not os.path.exists(path):
            problems.append((part["name"], "missing"))
            continue
        size = os.path.getsize(path)
        if size != part["size"]:
            problems.append((part["name"],
                             "wrong size (%s, expected %s)" % (human(size), human(part["size"]))))
            continue
        if deep:
            if progress:
                say("  checking %s (%d/%d)   " % (part["name"], i, len(manifest["parts"])), end="\r")
            if sha256_file(path) != part["sha256"]:
                problems.append((part["name"], "checksum mismatch - re-send this part"))
                continue
        ok.append(part["name"])
    if deep and progress:
        say(" " * 60, end="\r")
    return ok, problems


def say(msg, end="\n"):
    sys.stdout.write(msg + end)
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_pack(args):
    src = os.path.abspath(args.source)
    if not os.path.isdir(src):
        raise Usage("source folder does not exist: %s" % src)

    out_dir = os.path.abspath(args.output or os.getcwd())
    base_name = args.name or os.path.basename(src.rstrip(os.sep)) or "archive"
    codec = pick_codec(args.codec)
    chunk_size = int(args.chunk_mb * 1024 * 1024)
    if chunk_size <= 0:
        raise Usage("--chunk-mb must be greater than 0")

    manifest_path = os.path.join(out_dir, base_name + MANIFEST_SUFFIX)
    existing = [n for n in (os.listdir(out_dir) if os.path.isdir(out_dir) else [])
                if n.startswith(base_name + ".part") or n == os.path.basename(manifest_path)]

    files, raw_total = scan_source(src)
    say("Source : %s" % src)
    say("Content: %d files, %s" % (files, human(raw_total)))
    say("Output : %s" % out_dir)
    say("Codec  : %s, level %d, %d MB chunks" % (codec, args.level, args.chunk_mb))

    if args.dry_run:
        est = max(1, int(raw_total * 0.45 / chunk_size) + 1)
        say("\nDry run - nothing was written.")
        say("Roughly %d chunk(s) expected (actual count depends on how well the data compresses)." % est)
        return 0

    if existing and not args.force:
        raise Usage(
            "%d file(s) named %s.* already exist in %s. Move them aside, pick "
            "another --name, or pass --force to overwrite." % (len(existing), base_name, out_dir)
        )

    os.makedirs(out_dir, exist_ok=True)
    for name in existing:
        os.remove(os.path.join(out_dir, name))

    started = time.time()
    chunks = ChunkWriter(out_dir, base_name, chunk_size)
    packer = CompressingWriter(chunks, codec, args.level)
    done = [0, 0]  # files, bytes

    def report(tarinfo):
        if tarinfo.isdir():
            return tarinfo
        done[0] += 1
        if done[0] % 50 == 0 or done[0] == files:
            pct = (done[0] / files * 100) if files else 100
            say("  packed %d/%d files (%.0f%%)   " % (done[0], files, pct), end="\r")
        return tarinfo

    say("")
    root_name = os.path.basename(src.rstrip(os.sep))
    with tarfile.open(fileobj=packer, mode="w|", bufsize=READ_BLOCK) as tar:
        tar.add(src, arcname=root_name, filter=report)
    packer.close()
    chunks.close()
    say(" " * 60, end="\r")

    manifest = {
        "tool": "resilient-folder-packer",
        "format": 1,
        "version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "name": base_name,
        "root": root_name,
        "codec": codec,
        "level": args.level,
        "chunk_size": chunk_size,
        "source_files": files,
        "source_bytes": raw_total,
        "stream_bytes": packer.raw_bytes,
        "stream_sha256": packer.hash.hexdigest(),
        "packed_bytes": chunks.total_bytes,
        "parts": chunks.parts,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    elapsed = max(time.time() - started, 0.001)
    ratio = (chunks.total_bytes / raw_total * 100) if raw_total else 100
    say("Packed %s into %d chunk(s) of up to %d MB in %.1fs (%s/s)."
        % (human(chunks.total_bytes), len(chunks.parts), args.chunk_mb, elapsed, human(raw_total / elapsed)))
    say("Compressed to %.1f%% of the original size." % ratio)
    say("")
    manifest_name = os.path.basename(manifest_path)
    hint = manifest_name if " " not in manifest_name else '"%s"' % manifest_name
    say("Send the whole folder over. On the other side run:")
    say("  python rfpack.py unpack %s --into <destination>" % hint)
    return 0


def cmd_verify(args):
    manifest_path = find_manifest(args.target)
    manifest = load_manifest(manifest_path)
    say("Archive: %s (%d part(s), %s)"
        % (manifest["name"], len(manifest["parts"]), human(manifest["packed_bytes"])))
    ok, problems = check_parts(manifest_path, manifest, deep=not args.quick)
    if not problems:
        say("All %d part(s) are present and intact. Ready to unpack." % len(ok))
        return 0
    say("%d of %d part(s) are good. Re-send only these:" % (len(ok), len(manifest["parts"])))
    for name, reason in problems:
        say("  %-28s %s" % (name, reason))
    return 1


def cmd_info(args):
    manifest_path = find_manifest(args.target)
    m = load_manifest(manifest_path)
    say("Name       : %s" % m["name"])
    say("Created    : %s (rfpack %s)" % (m["created"], m.get("version", "?")))
    say("Codec      : %s level %s" % (m["codec"], m.get("level")))
    say("Restores to: %s%s" % (m.get("root") or m["name"], os.sep))
    say("Source     : %d files, %s" % (m["source_files"], human(m["source_bytes"])))
    say("Packed     : %s in %d chunk(s) of up to %s"
        % (human(m["packed_bytes"]), len(m["parts"]), human(m["chunk_size"])))
    if m["source_bytes"]:
        say("Ratio      : %.1f%% of original" % (m["packed_bytes"] / m["source_bytes"] * 100))
    if m["codec"] not in available_codecs():
        say("")
        say("Warning: this machine cannot read %s archives. Install it with "
            "`pip install zstandard` before unpacking." % m["codec"])
    return 0


def _safe_members(tar, dest_root):
    dest_root = os.path.abspath(dest_root)
    for member in tar:
        target = os.path.abspath(os.path.join(dest_root, member.name))
        if not (target == dest_root or target.startswith(dest_root + os.sep)):
            raise Usage("archive contains an unsafe path (%r) - refusing to extract." % member.name)
        if member.issym() or member.islnk():
            continue
        yield member


def cmd_unpack(args):
    manifest_path = find_manifest(args.target)
    manifest = load_manifest(manifest_path)
    folder = os.path.dirname(os.path.abspath(manifest_path))
    dest = os.path.abspath(args.into)

    if manifest["codec"] not in available_codecs():
        raise Usage(
            "this archive is %s-compressed and this machine has no %s support. "
            "Run `pip install zstandard`, or unpack it on a machine that has it."
            % (manifest["codec"], manifest["codec"])
        )

    say("Archive: %s (%d part(s))" % (manifest["name"], len(manifest["parts"])))
    if not args.skip_verify:
        _ok, problems = check_parts(manifest_path, manifest, deep=True)
        if problems:
            say("Cannot unpack - %d part(s) still need to be re-sent:" % len(problems))
            for name, reason in problems:
                say("  %-28s %s" % (name, reason))
            return 1
        say("All parts verified.")

    probe = os.path.join(dest, manifest.get("root") or manifest["name"])
    if os.path.exists(probe) and not args.force:
        raise Usage(
            "%s already exists. Unpack somewhere else, or pass --force to write "
            "into it (existing files with the same names are overwritten)." % probe
        )

    if args.dry_run:
        say("Dry run - would extract %d files (%s) into %s"
            % (manifest["source_files"], human(manifest["source_bytes"]), dest))
        return 0

    os.makedirs(dest, exist_ok=True)
    started = time.time()
    stream = _PartReader(folder, manifest["parts"])
    raw = _DecompressReader(stream, manifest["codec"])
    with tarfile.open(fileobj=raw, mode="r|", bufsize=READ_BLOCK) as tar:
        count = 0
        for member in _safe_members(tar, dest):
            tar.extract(member, dest)
            if not member.isdir():
                count += 1
                if count % 50 == 0:
                    say("  restored %d files   " % count, end="\r")
    say(" " * 60, end="\r")

    elapsed = max(time.time() - started, 0.001)
    say("Restored %d files into %s in %.1fs." % (count, dest, elapsed))
    if raw.digest() != manifest["stream_sha256"]:
        say("Warning: the rebuilt stream does not match the recorded checksum. "
            "Re-verify the parts with `rfpack verify`.")
        return 1
    say("Checksum matches the original archive stream.")
    return 0


class _PartReader(io.RawIOBase):
    """Reads the part files back as one continuous stream."""

    def __init__(self, folder, parts):
        self.paths = [os.path.join(folder, p["name"]) for p in parts]
        self.index = 0
        self.fh = None

    def readable(self):
        return True

    def readinto(self, buf):
        while True:
            if self.fh is None:
                if self.index >= len(self.paths):
                    return 0
                self.fh = open(self.paths[self.index], "rb")
                self.index += 1
            n = self.fh.readinto(buf)
            if n:
                return n
            self.fh.close()
            self.fh = None


class _DecompressReader(io.RawIOBase):
    def __init__(self, source, codec):
        self.source = source
        self.codec = decompressor(codec)
        self.buffer = b""
        self.eof = False
        self.hash = hashlib.sha256()

    def readable(self):
        return True

    def digest(self):
        return self.hash.hexdigest()

    def readinto(self, buf):
        while not self.buffer and not self.eof:
            block = self.source.read(READ_BLOCK)
            if not block:
                self.eof = True
                if hasattr(self.codec, "flush"):
                    try:
                        self.buffer += self.codec.flush()
                    except Exception:
                        pass
                break
            self.buffer += self.codec.decompress(block)
        if not self.buffer:
            return 0
        n = min(len(buf), len(self.buffer))
        buf[:n] = self.buffer[:n]
        self.hash.update(self.buffer[:n])
        self.buffer = self.buffer[n:]
        return n


def cmd_doctor(_args):
    say("Resilient Folder Packer %s" % __version__)
    say("Python  : %s" % sys.version.split()[0])
    say("Platform: %s" % sys.platform)
    say("")
    say("Codecs available here:")
    for name, detail in available_codecs().items():
        say("  %-6s %s" % (name, detail))
    if "zstd" not in available_codecs():
        say("")
        say("Zstandard is not installed, so `--codec auto` falls back to gzip. "
            "For the fastest packing run: pip install zstandard")
    say("")
    say("Free space in %s: %s" % (os.getcwd(), human(shutil.disk_usage(os.getcwd()).free)))
    return 0


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="rfpack",
        description="Pack a folder into verifiable chunks, move them over an "
                    "unstable link, and rebuild them on the other side.",
        epilog="Single file, no installer. Copy rfpack.py onto any machine with "
               "Python 3.8+ and run it.",
    )
    p.add_argument("--version", action="version", version="rfpack " + __version__)
    subs = p.add_subparsers(dest="command")

    pack = subs.add_parser("pack", help="compress a folder into numbered chunks")
    pack.add_argument("source", help="folder to pack")
    pack.add_argument("-o", "--output", help="where to write the chunks (default: current folder)")
    pack.add_argument("-n", "--name", help="archive base name (default: the folder's name)")
    pack.add_argument("--chunk-mb", type=int, default=DEFAULT_CHUNK_MB,
                      help="chunk size in MB (default: %d)" % DEFAULT_CHUNK_MB)
    pack.add_argument("--codec", default="auto", choices=["auto", "zstd", "xz", "gz", "raw"],
                      help="compression backend (default: auto - zstd when available)")
    pack.add_argument("--level", type=int, default=DEFAULT_LEVEL,
                      help="compression level, higher is smaller and slower (default: %d)" % DEFAULT_LEVEL)
    pack.add_argument("--force", action="store_true", help="overwrite existing chunks with the same name")
    pack.add_argument("--dry-run", action="store_true", help="show what would be packed, write nothing")
    pack.set_defaults(func=cmd_pack)

    unpack = subs.add_parser("unpack", help="verify the chunks and restore the folder")
    unpack.add_argument("target", help="the .rfpack.json manifest, or the folder holding it")
    unpack.add_argument("--into", required=True, help="destination folder")
    unpack.add_argument("--force", action="store_true", help="write into an existing destination folder")
    unpack.add_argument("--skip-verify", action="store_true",
                        help="skip the checksum pass (faster, but a damaged part fails late)")
    unpack.add_argument("--dry-run", action="store_true", help="report what would be restored, write nothing")
    unpack.set_defaults(func=cmd_unpack)

    verify = subs.add_parser("verify", help="list which chunks are missing or damaged")
    verify.add_argument("target", help="the .rfpack.json manifest, or the folder holding it")
    verify.add_argument("--quick", action="store_true", help="check names and sizes only, skip checksums")
    verify.set_defaults(func=cmd_verify)

    info = subs.add_parser("info", help="show what an archive contains")
    info.add_argument("target", help="the .rfpack.json manifest, or the folder holding it")
    info.set_defaults(func=cmd_info)

    doctor = subs.add_parser("doctor", help="show which codecs this machine supports")
    doctor.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except Usage as exc:
        say("Error: %s" % exc)
        return 2
    except KeyboardInterrupt:
        say("\nStopped. Nothing was left half-written except the chunk in progress; "
            "re-run pack to start over.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
