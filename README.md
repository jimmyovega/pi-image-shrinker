# Pi Image Shrinker

A Windows 11 GUI tool that shrinks Raspberry Pi SD card disk images (`.img` / `.iso`) by removing unused free space — turning a 32 GB or 64 GB capture down to only the space the filesystem actually uses.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## The Problem

When you image a Raspberry Pi SD card with a tool like Win32DiskImager or `dd`, the output file is the full size of the card — even if only 2 GB of a 64 GB card is used. Pi Image Shrinker trims that image to just the used space.

## Requirements

- **Windows 10** (build 19041+) or **Windows 11**
- **WSL** (Windows Subsystem for Linux) with Ubuntu
  ```powershell
  # Run in an elevated PowerShell, then restart
  wsl --install
  ```
- **Python 3.8+** — only needed to run from source or build the `.exe`
- No third-party Python packages required

## Quick Start

### Run from source
```bash
python pi_shrinker.py
```

### Build a standalone `.exe`
```bat
build.bat
```
The finished executable will be at `dist\PiImageShrinker.exe` — no Python needed on the target machine.

## How It Works

Raspberry Pi images use MBR partition layout with two partitions:

| Partition | Type | Purpose |
|-----------|------|---------|
| 1 | FAT32 | Boot (`/boot`) |
| 2 | ext4 | Root filesystem (`/`) |

The tool performs these steps:

1. **Parse the MBR** (pure Python, `struct` module) to find the root partition's LBA start and size
2. **In WSL:**
   - Mount the image as a loop device (`losetup -f --show -P`)
   - Check and fix the ext4 filesystem (`e2fsck -f -y`)
   - Shrink to minimum size (`resize2fs -M`)
   - Read new block count and size (`tune2fs -l`)
   - Detach the loop device
3. **Back in Python:**
   - Patch the MBR `lba_size` field for the root partition
   - Add 8192 sectors of safety padding
   - Truncate the file at the new boundary

## Troubleshooting

**"WSL is not installed or not responding"**
> Run `wsl --install` in an elevated PowerShell and restart your PC.

**"losetup did not return a valid loop device"**
> WSL is likely waiting for a `sudo` password interactively. Open a WSL terminal first, run any `sudo` command to cache credentials, then retry within 15 minutes. Alternatively, configure passwordless `sudo` for `losetup`, `e2fsck`, `resize2fs`, and `tune2fs`.

**"Not a valid MBR disk image"**
> The image may be compressed. Decompress `.img.xz` or `.img.gz` files first with 7-Zip before shrinking.

**resize2fs fails**
> The root partition may not be ext4. Only standard Raspberry Pi OS (Raspbian) images are supported.

## Files

| File | Description |
|------|-------------|
| `pi_shrinker.py` | Main Python source — run directly or build to `.exe` |
| `build.bat` | Compiles to `dist\PiImageShrinker.exe` via PyInstaller |
| `README.md` | This file |
