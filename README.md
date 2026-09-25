# Resilient Folder Packer

A blazing-fast Zstd archiver built to transfer thousands of small files over unstable WiFi, LAN, VPN, or Tailscale connections. Auto-splits into chunks so you never lose transfer progress.

Copying 2,800 small Excel files across a network share is slow for a reason: every single file costs a round trip. Packing them into one archive fixes that — until the WiFi or the VPN drops at 90% and you start the whole transfer again.

`rfpack` packs the folder into numbered chunks with a checksum for each one. If the link dies mid-transfer, you re-send the two chunks that did not make it, not the 480 MB.

---

## Why it exists

A real case: two folders of purchase-order spreadsheets on an office file server — 2,866 files, about 481 MB — had to go from the server to a PC over LAN, then from that PC to a laptop over Tailscale. Over SMB the per-file overhead made it crawl, and every dropped connection meant starting over.

Packed with `rfpack`, the same folders become a handful of chunk files that copy at full link speed, and a dropped connection costs one chunk.

## What it does

- **One stream, not thousands of files.** The folder is tarred and compressed in a single pass, so the network sees a few large files instead of thousands of tiny ones.
- **Auto-chunking.** The output is split into fixed-size parts (64 MB by default). Re-send only the parts that failed.
- **Per-chunk SHA-256.** `rfpack verify` tells you exactly which parts are missing or damaged, before you try to unpack.
- **Zstandard when it is there, standard library when it is not.** With `zstandard` installed you get Zstd speed; without it, the same file still works using gzip/xz from the Python standard library.
- **Portable.** One `.py` file. No installer, no admin rights, no dependencies required.

## Get it

Grab the repository however you prefer — there is nothing to build:

```bash
git clone https://github.com/kelvianlab/Resilient-Folder-Packer
cd Resilient-Folder-Packer
python rfpack.py doctor
```

No git? Open the repository page, click **Code → Download ZIP**, extract it, and
run the same command inside the extracted folder. The only file that actually
matters is `rfpack.py`; the launchers and docs are convenience.

## Run it without installing anything

Copy `rfpack.py` (and the matching launcher, if you like) onto a USB stick, a network share, or straight into the folder you are packing. Any machine with Python 3.8 or newer can run it as-is:

```bash
python rfpack.py doctor
```

| Platform | Command |
|---|---|
| Windows | `rfpack.cmd pack "D:\PURCHASE ORDER\PO NON PPN"` |
| Linux / macOS | `./rfpack.sh pack ~/purchase-order` |
| Anywhere | `python rfpack.py pack <folder>` |

`rfpack doctor` prints which codecs the machine supports and how much free disk space you have — run it first on an unfamiliar PC.

For maximum speed, optionally add Zstandard on the machine that does the packing:

```bash
pip install zstandard
```

Without it, `--codec auto` falls back to gzip and everything else works the same.

## Usage

### Pack

```bash
python rfpack.py pack "D:\PURCHASE ORDER\PO NON PPN" -o E:\transfer --chunk-mb 64
```

```
Source : D:\PURCHASE ORDER\PO NON PPN
Content: 1337 files, 212.7 MB
Output : E:\transfer
Codec  : zstd, level 6, 64 MB chunks

Packed 38.4 MB into 1 chunk(s) of up to 64 MB in 4.2s (50.6 MB/s).
Compressed to 18.1% of the original size.
```

You get:

```
PO NON PPN.part001
PO NON PPN.part002
PO NON PPN.rfpack.json     <- the manifest: chunk list + checksums
```

Copy that folder across however you like — `scp`, a Tailscale share, a USB stick, Explorer drag-and-drop.

### Verify on the far side

```bash
python rfpack.py verify "PO NON PPN.rfpack.json"
```

```
Archive: PO NON PPN (8 part(s), 483.1 MB)
6 of 8 part(s) are good. Re-send only these:
  PO NON PPN.part004           missing
  PO NON PPN.part007           checksum mismatch - re-send this part
```

Re-copy just those two files and run `verify` again.

### Unpack

```bash
python rfpack.py unpack "PO NON PPN.rfpack.json" --into "C:\restored"
```

Every chunk is checksummed before extraction, and the rebuilt stream is checked against the original hash afterwards. If a chunk is still bad, it refuses to extract rather than leaving you a half-restored folder.

### All commands

| Command | What it does |
|---|---|
| `pack <folder>` | Compress a folder into numbered chunks + a manifest |
| `verify <manifest>` | List which chunks are missing or damaged |
| `unpack <manifest> --into <dest>` | Verify the chunks and restore the folder |
| `info <manifest>` | Show what an archive holds, without unpacking it |
| `doctor` | Show available codecs, Python version, free disk space |

Useful flags:

| Flag | Applies to | Meaning |
|---|---|---|
| `--chunk-mb N` | pack | Chunk size in MB (default 64). Smaller on a very flaky link. |
| `--codec auto\|zstd\|xz\|gz\|raw` | pack | Backend. `auto` picks Zstd when available, else gzip. |
| `--level N` | pack | Compression level — higher is smaller and slower (default 6). |
| `--dry-run` | pack, unpack | Report what would happen, write nothing. |
| `--force` | pack, unpack | Overwrite existing chunks / write into an existing folder. |
| `--quick` | verify | Check names and sizes only, skip checksums. |
| `--skip-verify` | unpack | Skip the checksum pass when you already ran `verify`. |

## Picking a chunk size

| Link | Suggested `--chunk-mb` |
|---|---|
| Gigabit LAN, stable | 256 |
| Office WiFi | 64 (default) |
| Tailscale / VPN over mobile or flaky WiFi | 16–32 |

Smaller chunks mean less work lost per drop, and more files to keep track of. 64 MB is a sensible middle.

## What it does not do

- It does not encrypt. The chunks are plain compressed data — use an encrypted transport, or encrypt the chunks yourself, if the content is sensitive.
- It does not move files for you. It makes the payload transfer-friendly; the copying is still yours to do with whatever tool you already use.
- It does not follow symlinks, and refuses to extract any archive entry that points outside the destination folder.
- It stores file contents and timestamps, not Windows ACLs or alternate data streams.

## Requirements

- Python 3.8 or newer. Nothing else is required.
- `zstandard` (optional) for the fastest codec.

## Safety notes

- `pack` refuses to overwrite chunks that already exist unless you pass `--force`.
- `unpack` refuses to write into an existing destination folder unless you pass `--force`.
- Both support `--dry-run`.
- Extraction rejects path-traversal entries and skips symlinks, so an archive from someone else cannot write outside the folder you chose.

## License

MIT — see [LICENSE](LICENSE).
