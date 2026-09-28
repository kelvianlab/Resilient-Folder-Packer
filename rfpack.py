#!/usr/bin/env python3
"""Resilient Folder Packer - pack a folder into verifiable chunks, move them over
any flaky link, and rebuild without redoing the whole transfer.

Single file, no installer, standard library only. Uses Zstandard when the
optional `zstandard` package is present, and falls back to stdlib compression
otherwise so the same file still runs on a machine you just plugged into.

A long pack must never die halfway because of one bad file: anything that
cannot be read is skipped, logged, and reported at the end, and the run
carries on.
"""

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import time

__version__ = "1.2.0"

MANIFEST_SUFFIX = ".rfpack.json"
PACK_LOG_SUFFIX = ".rfpack-log.txt"
UNPACK_LOG_SUFFIX = ".unpack-log.txt"
DEFAULT_CHUNK_MB = 64
DEFAULT_LEVEL = 6
READ_BLOCK = 1024 * 1024
TAR_OVERHEAD_PER_ENTRY = 1536
SPACE_MARGIN = 64 * 1024 * 1024
REPORT_LIMIT = 20

# Windows reports a full disk as winerror 112 (ERROR_DISK_FULL) or 39
# (ERROR_HANDLE_DISK_FULL) rather than, or as well as, ENOSPC.
DISK_FULL_WINERRORS = (39, 112)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def winlong(path):
    """Return a path Windows accepts past the classic 260-character limit.

    A deeply nested folder tree with long, descriptive names crosses that
    limit easily, and without the \\\\?\\ prefix Windows reports such files as
    missing even though they are there. No-op on other systems.
    """
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path.lstrip("\\")
    return "\\\\?\\" + path


def shown(path):
    """Strip the long-path prefix again for anything shown to a person."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_inside(child, parent):
    child = os.path.normcase(os.path.abspath(child))
    parent = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return child == parent or child.startswith(parent + os.sep)


def is_link_dir(path):
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def archive_root_name(src):
    name = os.path.basename(src.rstrip("\\/"))
    if not name:
        drive = os.path.splitdrive(src)[0].strip("\\/:")
        name = (drive + "_drive") if drive else "archive"
    return name.replace(":", "_")


def free_space(path):
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def is_disk_full(exc):
    return (getattr(exc, "errno", None) == errno.ENOSPC
            or getattr(exc, "winerror", None) in DISK_FULL_WINERRORS)


def why(exc):
    return getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__


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
    if codec == "zstd":
        import zstandard
        return zstandard.ZstdCompressor(level=level, threads=-1).compressobj()
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
# Streams
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
        self._name = None
        self.total_bytes = 0

    def writable(self):
        return True

    def _open_next(self):
        self._name = "%s.part%03d" % (self.base_name, len(self.parts) + 1)
        self._fh = open(winlong(os.path.join(self.out_dir, self._name)), "wb")
        self._hash = hashlib.sha256()
        self._written = 0

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
            piece = view[:self.chunk_size - self._written]
            self._fh.write(piece)
            self._hash.update(piece)
            self._written += len(piece)
            self.total_bytes += len(piece)
            view = view[len(piece):]
            if self._written >= self.chunk_size:
                self._close_current()
        return len(data)

    def close(self):
        self._close_current()

    def abandon(self):
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None


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


class ExactReader:
    """Feeds tarfile exactly `size` bytes of a source file, whatever happens.

    The tar header is written before the data, so a file that shrinks or
    throws a read error halfway would otherwise leave a corrupt stream and
    kill the whole run. Missing bytes are zero-filled instead and the file
    is reported as damaged, so every other file in the archive stays good.
    """

    def __init__(self, fh, size, first):
        self.fh = fh
        self.left = size
        self.first = first
        self.first_pos = 0
        self.problem = None

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n is None or n < 0 or n > self.left:
            n = self.left
        data = b""
        if self.first_pos < len(self.first):
            data = self.first[self.first_pos:self.first_pos + n]
            self.first_pos += len(data)
        while len(data) < n and self.problem is None:
            try:
                more = self.fh.read(n - len(data))
            except OSError as exc:
                self.problem = "read error part-way through: %s" % why(exc)
                break
            if not more:
                self.problem = "file got shorter while it was being packed"
                break
            data += more
        if len(data) < n:
            data += b"\0" * (n - len(data))
        self.left -= n
        return data


class _PartReader(io.RawIOBase):
    """Reads the part files back as one continuous stream."""

    def __init__(self, folder, parts):
        self.paths = [winlong(os.path.join(folder, p["name"])) for p in parts]
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
        self.pos = 0
        self.eof = False
        self.hash = hashlib.sha256()

    def readable(self):
        return True

    def digest(self):
        return self.hash.hexdigest()

    def readinto(self, buf):
        while self.pos >= len(self.buffer) and not self.eof:
            block = self.source.read(READ_BLOCK)
            if block:
                try:
                    self.buffer, self.pos = self.codec.decompress(block), 0
                except Exception as exc:
                    raise Usage("the archive data is damaged (%s). Run verify, re-send any "
                                "part it names, then unpack again." % exc)
                continue
            self.eof = True
            with contextlib.suppress(Exception):
                self.buffer, self.pos = self.codec.flush(), 0
        if self.pos >= len(self.buffer):
            return 0
        n = min(len(buf), len(self.buffer) - self.pos)
        piece = self.buffer[self.pos:self.pos + n]
        buf[:n] = piece
        self.hash.update(piece)
        self.pos += n
        return n


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def say(msg, end="\n"):
    sys.stdout.write(msg + end)
    sys.stdout.flush()


def human(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return "%.1f %s" % (num, unit) if unit != "B" else "%d B" % num
        num /= 1024.0


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


def tool_command():
    if getattr(sys, "frozen", False):
        return ".\\rfpack.exe" if os.name == "nt" else "./rfpack"
    return "python rfpack.py"


def tool_file():
    return "rfpack.exe" if getattr(sys, "frozen", False) and os.name == "nt" else "rfpack.py"


class Progress:
    def __init__(self, verb, total_files, total_bytes):
        self.verb = verb
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.started = time.time()
        self.last = 0.0

    def update(self, files, nbytes, force=False):
        now = time.time()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        pct = nbytes / self.total_bytes * 100 if self.total_bytes else 100.0
        elapsed = now - self.started
        eta = ""
        if nbytes and self.total_bytes and elapsed > 3:
            eta = "  ETA %s" % duration((self.total_bytes - nbytes) / (nbytes / elapsed))
        say("  %s %s/%s files  %s of %s (%.0f%%)%s   " % (
            self.verb, format(files, ","), format(self.total_files, ","),
            human(nbytes), human(self.total_bytes), min(pct, 100.0), eta), end="\r")

    def clear(self):
        say(" " * 79, end="\r")


def report_list(title, items, log_path):
    say("")
    say(title)
    for path, reason in items[:REPORT_LIMIT]:
        say("  %s" % path)
        say("      -> %s" % reason)
    if len(items) > REPORT_LIMIT:
        say("  ... and %d more." % (len(items) - REPORT_LIMIT))
    say("Full list: %s" % log_path)


def write_log(path, sections):
    with open(winlong(path), "w", encoding="utf-8") as fh:
        fh.write("Resilient Folder Packer %s - %s\n" % (__version__, time.strftime("%Y-%m-%d %H:%M:%S")))
        for title, items in sections:
            if not items:
                continue
            fh.write("\n%s (%d)\n" % (title, len(items)))
            for path, reason in items:
                fh.write("  %s\n      -> %s\n" % (path, reason))


@contextlib.contextmanager
def keep_awake():
    """Stop Windows from going to sleep while a long job runs."""
    kernel32 = None
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
        except Exception:
            kernel32 = None
    try:
        yield
    finally:
        if kernel32 is not None:
            with contextlib.suppress(Exception):
                kernel32.SetThreadExecutionState(0x80000000)


# --------------------------------------------------------------------------- #
# Manifest helpers
# --------------------------------------------------------------------------- #

def sha256_file(path):
    digest = hashlib.sha256()
    with open(winlong(path), "rb") as fh:
        for block in iter(lambda: fh.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path):
    try:
        with open(winlong(path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        raise Usage("%s is damaged or not a manifest - re-send it from the source PC." % path)
    if data.get("tool") != "resilient-folder-packer":
        raise Usage("%s is not a Resilient Folder Packer manifest." % path)
    return data


def find_manifest(target):
    if os.path.isfile(target) and target.endswith(MANIFEST_SUFFIX):
        return target
    if os.path.isdir(target):
        hits = sorted(n for n in os.listdir(target) if n.endswith(MANIFEST_SUFFIX))
        if not hits:
            raise Usage("no %s manifest found in %s" % (MANIFEST_SUFFIX, os.path.abspath(target)))
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
    total = len(manifest["parts"])
    for i, part in enumerate(manifest["parts"], 1):
        path = os.path.join(folder, part["name"])
        if not os.path.exists(winlong(path)):
            problems.append((part["name"], "missing"))
            continue
        size = os.path.getsize(winlong(path))
        if size != part["size"]:
            problems.append((part["name"], "wrong size (%s, expected %s) - re-send this part"
                             % (human(size), human(part["size"]))))
            continue
        if deep:
            if progress:
                say("  checking %s (%d/%d)   " % (part["name"], i, total), end="\r")
            try:
                digest = sha256_file(path)
            except OSError as exc:
                problems.append((part["name"], "cannot be read: %s" % why(exc)))
                continue
            if digest != part["sha256"]:
                problems.append((part["name"], "checksum mismatch - re-send this part"))
                continue
        ok.append(part["name"])
    if deep and progress:
        say(" " * 79, end="\r")
    return ok, problems


# --------------------------------------------------------------------------- #
# Pack
# --------------------------------------------------------------------------- #

def collect_entries(src, progress=True):
    """Walk the source once.

    Returns (root_name, entries, total_bytes, problems). Each entry is
    (arcname, path, is_dir, size). Unreadable folders and links are recorded
    in problems instead of stopping the walk.
    """
    root = winlong(src)
    root_name = archive_root_name(src)
    entries, problems = [], []
    total = 0
    last = [time.time()]

    def on_error(exc):
        problems.append((shown(exc.filename or root), "folder cannot be opened: %s" % why(exc)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        rel = dirpath[len(root):].strip("\\/")
        arc_dir = root_name + ("/" + rel.replace(os.sep, "/") if rel else "")
        entries.append((arc_dir, dirpath, True, 0))

        keep = []
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            if is_link_dir(path):
                problems.append((shown(path), "shortcut/link to another folder - not followed"))
            else:
                keep.append(name)
        dirnames[:] = keep

        if progress and time.time() - last[0] > 1:
            last[0] = time.time()
            say("  scanning... %s files found so far   " % format(len(entries), ","), end="\r")

        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError as exc:
                problems.append((shown(path), "cannot be read: %s" % why(exc)))
                continue
            if stat.S_ISLNK(st.st_mode):
                problems.append((shown(path), "symbolic link - not packed"))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            entries.append((arc_dir + "/" + name, path, False, st.st_size))
            total += st.st_size
    if progress:
        say(" " * 79, end="\r")
    return root_name, entries, total, problems


def _dir_info(arcname, path):
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    try:
        info.mtime = os.stat(path).st_mtime
    except OSError:
        info.mtime = time.time()
    return info


def _add_file(tar, arcname, path):
    """Add one file. Returns (added_bytes, skip_reason, damage_reason)."""
    try:
        fh = open(path, "rb")
    except OSError as exc:
        return 0, "cannot be opened: %s" % why(exc), None
    with fh:
        try:
            st = os.fstat(fh.fileno())
            first = fh.read(min(st.st_size, READ_BLOCK)) if st.st_size else b""
        except OSError as exc:
            return 0, "cannot be read: %s" % why(exc), None
        info = tarfile.TarInfo(arcname)
        info.size = st.st_size
        info.mtime = st.st_mtime
        info.mode = (st.st_mode & 0o777) or 0o644
        try:
            info.tobuf(tar.format, tar.encoding, tar.errors)
        except (UnicodeError, ValueError) as exc:
            return 0, "file name cannot be stored: %s" % why(exc), None
        reader = ExactReader(fh, st.st_size, first)
        tar.addfile(info, reader)
        return st.st_size, None, reader.problem


def cmd_pack(args):
    src = os.path.abspath(args.source)
    if not os.path.isdir(winlong(src)):
        raise Usage("source folder does not exist: %s" % src)

    out_dir = os.path.abspath(args.output or os.getcwd())
    if is_inside(out_dir, src):
        raise Usage(
            "the output folder %s is inside the folder being packed, so the "
            "archive would end up packing itself. Pick an -o outside %s." % (out_dir, src)
        )
    codec = pick_codec(args.codec)
    chunk_size = int(args.chunk_mb * 1024 * 1024)
    if chunk_size <= 0:
        raise Usage("--chunk-mb must be greater than 0")

    root_name, entries, raw_total, problems = collect_entries(src)
    base_name = args.name or root_name
    files = sum(1 for e in entries if not e[2])
    say("Source : %s" % src)
    say("Content: %s files, %s" % (format(files, ","), human(raw_total)))
    say("Output : %s" % out_dir)
    say("Codec  : %s, level %d, %d MB chunks" % (codec, args.level, args.chunk_mb))
    if problems:
        say("Note   : %d item(s) cannot be read and will be skipped - listed at the end."
            % len(problems))

    worst_case = raw_total + len(entries) * TAR_OVERHEAD_PER_ENTRY + SPACE_MARGIN
    free = free_space(out_dir)
    space_ok = free >= worst_case

    if args.dry_run:
        say("")
        say("Dry run - nothing was written.")
        say("Free space on the output drive: %s (up to %s may be needed) - %s"
            % (human(free), human(worst_case), "OK" if space_ok else "NOT ENOUGH"))
        return 0

    if not space_ok and not args.skip_space_check:
        raise Usage(
            "not enough free space on the output drive: %s free, but up to %s "
            "may be needed (files that are already compressed - PDF, JPG, DWG, "
            "ZIP, video - barely shrink). Free up space or pick another -o. If "
            "you are sure the data compresses well, add --skip-space-check."
            % (human(free), human(worst_case))
        )

    manifest_path = os.path.join(out_dir, base_name + MANIFEST_SUFFIX)
    log_path = os.path.join(out_dir, base_name + PACK_LOG_SUFFIX)
    names = os.listdir(winlong(out_dir)) if os.path.isdir(winlong(out_dir)) else []
    existing = [n for n in names if n.startswith(base_name + ".part")
                or n in (os.path.basename(manifest_path), os.path.basename(log_path))]
    if existing and not args.force:
        raise Usage(
            "%d file(s) named %s.* already exist in %s. Move them aside, pick "
            "another --name, or add --force to overwrite them." % (len(existing), base_name, out_dir)
        )

    try:
        os.makedirs(winlong(out_dir), exist_ok=True)
        for name in existing:
            os.remove(winlong(os.path.join(out_dir, name)))
    except OSError as exc:
        raise Usage("cannot prepare the output folder %s: %s" % (out_dir, why(exc)))

    skipped = list(problems)
    damaged = []
    packed_files = 0
    packed_bytes = 0
    started = time.time()
    chunks = ChunkWriter(out_dir, base_name, chunk_size)
    packer = CompressingWriter(chunks, codec, args.level)
    progress = Progress("packed", files, raw_total)
    say("")

    try:
        with keep_awake(), tarfile.open(fileobj=packer, mode="w|", bufsize=READ_BLOCK,
                                        copybufsize=READ_BLOCK,
                                        format=tarfile.PAX_FORMAT) as tar:
            for arcname, path, is_dir, _size in entries:
                if is_dir:
                    try:
                        tar.addfile(_dir_info(arcname, path))
                    except (UnicodeError, ValueError) as exc:
                        skipped.append((shown(path), "folder name cannot be stored: %s" % why(exc)))
                    continue
                added, skip, damage = _add_file(tar, arcname, path)
                if skip:
                    skipped.append((shown(path), skip))
                    continue
                packed_files += 1
                packed_bytes += added
                if damage:
                    damaged.append((shown(path), damage))
                progress.update(packed_files, packed_bytes)
        packer.close()
        chunks.close()
    except OSError as exc:
        chunks.abandon()
        progress.clear()
        if is_disk_full(exc):
            raise Usage(
                "the output drive filled up after %s. Free up space (or pick "
                "another -o) and run the same command again with --force."
                % human(chunks.total_bytes))
        raise Usage("cannot write to the output folder %s: %s" % (out_dir, why(exc)))
    progress.update(packed_files, packed_bytes, force=True)
    say("")

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
        "source_files": packed_files,
        "source_bytes": packed_bytes,
        "stream_bytes": packer.raw_bytes,
        "stream_sha256": packer.hash.hexdigest(),
        "packed_bytes": chunks.total_bytes,
        "parts": chunks.parts,
        "skipped": [{"path": p, "reason": r} for p, r in skipped],
        "damaged": [{"path": p, "reason": r} for p, r in damaged],
    }
    try:
        with open(winlong(manifest_path), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        if skipped or damaged:
            write_log(log_path, [("NOT PACKED", skipped), ("PACKED BUT INCOMPLETE", damaged)])
    except OSError as exc:
        raise Usage("the data was packed but the manifest could not be written to %s: %s"
                    % (out_dir, why(exc)))

    elapsed = max(time.time() - started, 0.001)
    ratio = (chunks.total_bytes / packed_bytes * 100) if packed_bytes else 100
    say("")
    say("Packed %s of %s files (%s) into %d chunk(s) in %s."
        % (format(packed_files, ","), format(files, ","), human(packed_bytes),
           len(chunks.parts), duration(elapsed)))
    say("Compressed to %.1f%% of the original size." % ratio)

    if skipped:
        report_list("WARNING: %d item(s) could NOT be packed and are not in the archive:"
                    % len(skipped), skipped, log_path)
    if damaged:
        report_list("WARNING: %d file(s) changed or failed while being read; they are in "
                    "the archive but their contents are incomplete:" % len(damaged), damaged, log_path)

    say("")
    say("Send the whole folder %s over, with %s copied into it." % (out_dir, tool_file()))
    say("On the other side, open a terminal in that folder and run:")
    say("  %s unpack . --into <destination>" % tool_command())
    return 1 if (skipped or damaged) else 0


# --------------------------------------------------------------------------- #
# Verify / info
# --------------------------------------------------------------------------- #

def cmd_verify(args):
    manifest_path = find_manifest(args.target)
    manifest = load_manifest(manifest_path)
    say("Archive: %s (%d part(s), %s)"
        % (manifest["name"], len(manifest["parts"]), human(manifest["packed_bytes"])))
    with keep_awake():
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
    say("Content    : %s files, %s" % (format(m["source_files"], ","), human(m["source_bytes"])))
    say("Packed     : %s in %d chunk(s) of up to %s"
        % (human(m["packed_bytes"]), len(m["parts"]), human(m["chunk_size"])))
    if m["source_bytes"]:
        say("Ratio      : %.1f%% of original" % (m["packed_bytes"] / m["source_bytes"] * 100))
    if m.get("skipped"):
        say("Skipped    : %d item(s) could not be read when this was packed" % len(m["skipped"]))
    if m.get("damaged"):
        say("Incomplete : %d file(s) changed while being packed" % len(m["damaged"]))
    if m["codec"] not in available_codecs():
        say("")
        say("Warning: this machine cannot read %s archives. Use rfpack.exe, or "
            "install it with `pip install zstandard` before unpacking." % m["codec"])
    return 0


# --------------------------------------------------------------------------- #
# Unpack
# --------------------------------------------------------------------------- #

def member_target(dest, name):
    target = os.path.abspath(os.path.join(dest, name))
    if not is_inside(target, dest):
        raise Usage("archive contains an unsafe path (%r) - refusing to extract." % name)
    return target


def write_member(tar, member, target):
    path = winlong(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    source = tar.extractfile(member)
    with open(path, "wb") as out:
        shutil.copyfileobj(source, out, READ_BLOCK)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, (member.mode & 0o777) | 0o200)
    with contextlib.suppress(OSError, OverflowError, ValueError):
        os.utime(path, (member.mtime, member.mtime))


def cmd_unpack(args):
    manifest_path = find_manifest(args.target)
    manifest = load_manifest(manifest_path)
    folder = os.path.dirname(os.path.abspath(manifest_path))
    dest = os.path.abspath(args.into)

    if manifest["codec"] not in available_codecs():
        raise Usage(
            "this archive is %s-compressed and this machine has no %s support. "
            "Use rfpack.exe, which has it built in, or run `pip install zstandard`."
            % (manifest["codec"], manifest["codec"])
        )

    say("Archive: %s (%d part(s))" % (manifest["name"], len(manifest["parts"])))
    if not args.skip_verify:
        with keep_awake():
            _ok, problems = check_parts(manifest_path, manifest, deep=True)
        if problems:
            say("Cannot unpack - %d part(s) still need to be re-sent:" % len(problems))
            for name, reason in problems:
                say("  %-28s %s" % (name, reason))
            return 1
        say("All parts verified.")

    probe = os.path.join(dest, manifest.get("root") or manifest["name"])
    if os.path.exists(winlong(probe)) and not args.force:
        raise Usage(
            "%s already exists. Unpack somewhere else, or add --force to write "
            "into it (existing files with the same names are overwritten)." % probe
        )

    need = manifest["source_bytes"] + SPACE_MARGIN
    free = free_space(dest)
    if args.dry_run:
        say("Dry run - would restore %s files (%s) into %s"
            % (format(manifest["source_files"], ","), human(manifest["source_bytes"]), dest))
        say("Free space there: %s - %s" % (human(free), "OK" if free >= need else "NOT ENOUGH"))
        return 0
    if free < need and not args.skip_space_check:
        raise Usage(
            "not enough free space at %s: %s free, %s needed. Free up space or "
            "pick another --into." % (dest, human(free), human(need)))

    try:
        os.makedirs(winlong(dest), exist_ok=True)
    except OSError as exc:
        raise Usage("cannot create %s: %s" % (dest, why(exc)))

    failed = []
    folders = []
    count = 0
    nbytes = 0
    started = time.time()
    progress = Progress("restored", manifest["source_files"], manifest["source_bytes"])
    raw = _DecompressReader(_PartReader(folder, manifest["parts"]), manifest["codec"])

    with keep_awake():
        try:
            with tarfile.open(fileobj=raw, mode="r|", bufsize=READ_BLOCK) as tar:
                for member in tar:
                    target = member_target(dest, member.name)
                    if member.isdir():
                        try:
                            os.makedirs(winlong(target), exist_ok=True)
                            folders.append((target, member.mtime))
                        except OSError as exc:
                            if is_disk_full(exc):
                                raise
                            failed.append((target, "folder cannot be created: %s" % why(exc)))
                        continue
                    if not member.isreg():
                        failed.append((target, "not a regular file - skipped"))
                        continue
                    try:
                        write_member(tar, member, target)
                    except OSError as exc:
                        if is_disk_full(exc):
                            raise
                        failed.append((target, why(exc)))
                        continue
                    count += 1
                    nbytes += member.size
                    progress.update(count, nbytes)
            while raw.read(READ_BLOCK):
                pass
        except OSError as exc:
            progress.clear()
            if is_disk_full(exc):
                raise Usage("the destination drive filled up after %s. Free up space and "
                            "run the same command again with --force." % human(nbytes))
            raise Usage("cannot read the archive parts in %s: %s. Run verify first."
                        % (folder, why(exc)))
        except (tarfile.TarError, EOFError) as exc:
            progress.clear()
            raise Usage("the archive data is damaged (%s). Run verify, re-send any part "
                        "it names, then unpack again." % exc)

    for target, mtime in reversed(folders):
        with contextlib.suppress(OSError, OverflowError, ValueError):
            os.utime(winlong(target), (mtime, mtime))

    progress.update(count, nbytes, force=True)
    say("")
    say("")
    elapsed = max(time.time() - started, 0.001)
    say("Restored %s files (%s) into %s in %s."
        % (format(count, ","), human(nbytes), dest, duration(elapsed)))

    status = 0
    if raw.digest() != manifest["stream_sha256"]:
        say("WARNING: the rebuilt data does not match the recorded checksum. "
            "Run verify and re-send any part it names.")
        status = 1
    else:
        say("Checksum matches the original archive.")

    if failed:
        log_path = os.path.join(dest, manifest["name"] + UNPACK_LOG_SUFFIX)
        with contextlib.suppress(OSError):
            write_log(log_path, [("NOT RESTORED", failed)])
        report_list("WARNING: %d item(s) could NOT be restored:" % len(failed), failed, log_path)
        status = 1
    if manifest.get("skipped") or manifest.get("damaged"):
        say("")
        say("Note: when this archive was made, %d item(s) could not be packed and %d "
            "were incomplete. Run `info` or open the manifest to see which."
            % (len(manifest.get("skipped", [])), len(manifest.get("damaged", []))))
    return status


# --------------------------------------------------------------------------- #

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


def build_parser():
    p = argparse.ArgumentParser(
        prog="rfpack",
        description="Pack a folder into verifiable chunks, move them over an "
                    "unstable link, and rebuild them on the other side.",
        epilog="Exit codes: 0 done, 1 done with warnings (skipped or damaged "
               "items are listed), 2 nothing done - see the error, 130 interrupted.",
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
    pack.add_argument("--skip-space-check", action="store_true",
                      help="start even if the output drive may be too small")
    pack.set_defaults(func=cmd_pack)

    unpack = subs.add_parser("unpack", help="verify the chunks and restore the folder")
    unpack.add_argument("target", help="the .rfpack.json manifest, or the folder holding it")
    unpack.add_argument("--into", required=True, help="destination folder")
    unpack.add_argument("--force", action="store_true", help="write into an existing destination folder")
    unpack.add_argument("--skip-verify", action="store_true",
                        help="skip the checksum pass (faster, but a damaged part fails late)")
    unpack.add_argument("--dry-run", action="store_true", help="report what would be restored, write nothing")
    unpack.add_argument("--skip-space-check", action="store_true",
                        help="start even if the destination drive may be too small")
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
    # A file name the console code page cannot show must never crash a run
    # that is printing its warning list.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except Usage as exc:
        say("")
        say("Error: %s" % exc)
        return 2
    except KeyboardInterrupt:
        say("\nStopped. The source folder was not touched; run the same command "
            "again with --force to start over.")
        return 130
    except BrokenPipeError:
        # Output was piped into something that stopped reading, e.g. `| head`.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
