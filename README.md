# Resilient Folder Packer

A blazing-fast Zstd archiver built to transfer thousands of small files over unstable WiFi, LAN, VPN, or Tailscale connections. Auto-splits into chunks so you never lose transfer progress.

---

## The problem this solves

Copying 3,000 small Excel files across a network share is painfully slow, because every single file costs a network round trip. Zipping them first fixes that — until the WiFi or the VPN drops at 90% and you start the entire transfer over.

`rfpack` packs the folder into one compressed stream, then splits it into numbered chunks with a checksum for each one. If the connection dies mid-transfer, you re-send the two chunks that failed — not the whole 480 MB.

**A real case:** 2,866 purchase-order spreadsheets (481 MB) had to move from an office file server to a PC over LAN, then to a laptop over Tailscale. Over SMB it crawled, and every dropped connection meant starting again. Packed with `rfpack`, the same folders became a handful of chunk files that copy at full link speed.

---

# Quick start (Windows)

**You do not need Python, admin rights, or any installation.** Five steps.

### Step 1 — Download the tool

Go to the [latest release](../../releases/latest) and download **`rfpack.exe`**.

That single file is the entire program. Python and the compression libraries are already built into it.

### Step 2 — Put it where you can find it

Move `rfpack.exe` somewhere simple, for example `C:\rfpack\` or a USB stick. Avoid deep paths with lots of spaces — it just makes typing harder later.

### Step 3 — Open a terminal *in that folder*

This is the step people get wrong most often. The terminal has to be "standing inside" the folder that holds `rfpack.exe`.

1. Open that folder in File Explorer — you should see `rfpack.exe` listed.
2. Click the **address bar** at the top (where the folder path is shown).
3. Type `powershell` and press **Enter**.

A black window opens, already pointed at the right folder.

> If typing `powershell` opens another Explorer window instead of a terminal, type `powershell.exe` instead, or hold **Shift** and right-click an empty area of the folder, then choose **Open PowerShell window here**.

To confirm you are in the right place, type:

```powershell
dir
```

If `rfpack.exe` appears in the list, you are good.

### Step 4 — Check the tool runs

```powershell
.\rfpack.exe doctor
```

> **The `.\` at the front is required.** PowerShell refuses to run a program from the current folder without it. `rfpack.exe doctor` alone will fail; `.\rfpack.exe doctor` works.

You should see something like:

```
Resilient Folder Packer 1.1.0
Python  : 3.12.0
Platform: win32

Codecs available here:
  zstd   zstandard
  xz     lzma (stdlib)
  gz     zlib (stdlib)
  raw    no compression

Free space in C:\rfpack: 1.6 TB
```

If you see that, everything works.

### Step 5 — Pack your folder

```powershell
.\rfpack.exe pack "D:\PURCHASE ORDER\PO NON PPN" -o E:\kirim --chunk-mb 64
```

Replace `D:\PURCHASE ORDER\PO NON PPN` with the folder you want to send, and `E:\kirim` with where the chunks should be written.

> **Quotes matter.** If a path contains spaces, it must be wrapped in `"double quotes"`, exactly as above. Without them Windows reads it as several separate arguments and the command fails.

Not sure yet? Add `--dry-run` to the end. It reports what *would* happen and writes nothing.

---

# The full transfer, start to finish

This is the actual workflow the tool was built for.

### On the source PC

```powershell
.\rfpack.exe pack "D:\PURCHASE ORDER\PO NON PPN" -o E:\kirim --chunk-mb 64
```

`E:\kirim` now contains:

```
PO NON PPN.part001
PO NON PPN.part002
PO NON PPN.rfpack.json     <- the list of chunks and their checksums
```

**Copy `rfpack.exe` into `E:\kirim` as well.** The destination PC needs the tool to unpack, and this way the folder carries everything it needs — nothing to download on the other side.

### Move the folder

Copy `E:\kirim` across however you normally would: a USB stick, Explorer drag-and-drop, `scp`, a Tailscale share. The tool does not move files for you; it makes the payload transfer-friendly so your existing method stops choking.

### On the destination PC

Open a terminal inside the copied folder (Step 3 above), then:

```powershell
.\rfpack.exe verify .
```

The `.` means "this folder". You will get one of two answers:

```
All 8 part(s) are present and intact. Ready to unpack.
```

or

```
6 of 8 part(s) are good. Re-send only these:
  PO NON PPN.part004           missing
  PO NON PPN.part007           checksum mismatch - re-send this part
```

In the second case, re-copy **only those two files**, then run `verify` again. This is the whole point of the tool.

Once everything is intact:

```powershell
.\rfpack.exe unpack . --into C:\hasil
```

Every chunk is checked before extraction, and the rebuilt data is verified against the original checksum afterwards. If a chunk is still damaged, it refuses to extract rather than leaving you a half-restored folder.

---

# Command reference

| Command | What it does |
|---|---|
| `pack <folder>` | Compress a folder into numbered chunks + a manifest |
| `verify <folder>` | List which chunks are missing or damaged |
| `unpack <folder> --into <dest>` | Verify the chunks and restore the folder |
| `info <folder>` | Show what an archive holds, without unpacking it |
| `doctor` | Show available codecs, version, free disk space |

For `verify`, `unpack` and `info` you can point at the folder holding the chunks (`.` for the current one) — you do not need to type the manifest filename.

### Options

| Flag | Works with | Meaning |
|---|---|---|
| `--chunk-mb N` | pack | Chunk size in MB (default 64) |
| `--codec auto\|zstd\|xz\|gz\|raw` | pack | Compression backend. `auto` picks Zstd when available |
| `--level N` | pack | Compression level, higher is smaller and slower (default 6) |
| `--dry-run` | pack, unpack | Report what would happen, write nothing |
| `--force` | pack, unpack | Overwrite existing chunks / write into an existing folder |
| `--quick` | verify | Check names and sizes only, skip checksums |
| `--skip-verify` | unpack | Skip the checksum pass when you already ran `verify` |

### Choosing a chunk size

| Your connection | Suggested `--chunk-mb` |
|---|---|
| Gigabit LAN, stable | 256 |
| Office WiFi | 64 (default) |
| Tailscale / VPN over mobile or flaky WiFi | 16–32 |

Smaller chunks mean less work lost per dropped connection, and more files to keep track of. 64 MB is a sensible middle.

---

# Troubleshooting

### "Windows protected your PC" when running the .exe

Windows shows this for any program it hasn't seen before. `rfpack.exe` is not code-signed, because a signing certificate costs money this project does not spend.

Click **More info**, then **Run anyway**.

If you would rather not trust a binary you did not build, that is a fair instinct — use the Python script instead (see below). The full source is in this repository, and the `.exe` is built from it automatically by [GitHub Actions](../../actions), in public, with no manual step in between.

### "rfpack.exe is not recognized as the name of a cmdlet"

You left out the `.\` prefix. Use `.\rfpack.exe doctor`, not `rfpack.exe doctor`.

### "The system cannot find the path specified"

Your terminal is standing in the wrong folder. Type `dir` — if you do not see `rfpack.exe` in the list, go back to Step 3.

### The command fails on a folder name with spaces

Wrap the path in double quotes:

```powershell
.\rfpack.exe pack "D:\PURCHASE ORDER\PO NON PPN" -o E:\kirim
```

### `verify` says a part is damaged

That is the tool doing its job. Re-copy just the files it named, then run `verify` again. You do not need to re-send anything else.

### It says "this archive is zstd-compressed and this machine has no zstd support"

You are unpacking with the Python script on a machine without the `zstandard` package. Either run `pip install zstandard`, or use `rfpack.exe`, which has it built in.

---

# Other systems: the Python script

On Linux, macOS, or any machine that already has Python 3.8+, you can run the script directly — no build, no install:

```bash
git clone https://github.com/kelvianlab/Resilient-Folder-Packer
cd Resilient-Folder-Packer
python rfpack.py doctor
```

No git? Click **Code → Download ZIP** on the repository page and extract it. The only file that matters is `rfpack.py`.

| Platform | Command |
|---|---|
| Windows | `python rfpack.py pack "D:\PURCHASE ORDER\PO NON PPN"` |
| Linux / macOS | `python3 rfpack.py pack ~/purchase-order` |

Optional, for the fastest codec:

```bash
pip install zstandard
```

Without it the script falls back to gzip. Everything still works, just less quickly.

---

# What it does not do

- **It does not encrypt.** The chunks are plain compressed data. Use an encrypted transport, or encrypt the chunks yourself, if the contents are sensitive.
- **It does not move files for you.** It makes the payload transfer-friendly; the copying is still done with whatever tool you already use.
- **It does not follow symlinks**, and refuses to extract any entry that points outside the destination folder.
- **It stores file contents and timestamps**, not Windows ACLs or alternate data streams.

# Safety

- `pack` refuses to overwrite existing chunks unless you pass `--force`.
- `unpack` refuses to write into an existing destination folder unless you pass `--force`.
- Both support `--dry-run`.
- Extraction rejects path-traversal entries and skips symlinks, so an archive from someone else cannot write outside the folder you chose.

# Requirements

- **`rfpack.exe`**: nothing. Windows 10 or later, 64-bit.
- **`rfpack.py`**: Python 3.8 or newer. `zstandard` is optional.

# License

MIT — see [LICENSE](LICENSE).
