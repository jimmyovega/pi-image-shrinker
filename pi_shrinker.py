#!/usr/bin/env python3
"""
pi_shrinker.py — Raspberry Pi SD Card Image Shrinker
=====================================================
Trims a full-size Raspberry Pi .img/.iso disk image down to only the
space actually used by shrinking the ext4 root partition to its minimum
size, then truncating the file.

Requirements:
  - Windows 10/11 with WSL (Windows Subsystem for Linux) installed
  - The WSL distro must have: losetup, e2fsck, resize2fs, tune2fs
    (all standard in any Debian/Ubuntu WSL distro)
  - Python 3.8+  (no third-party packages needed)

How it works:
  1. Parse the MBR partition table (pure Python, struct module)
  2. Delegate filesystem work to WSL: losetup -> e2fsck -> resize2fs -> tune2fs
  3. Back in Python: patch the MBR lba_size field and truncate the file
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import struct
import os
import subprocess
import threading
import shutil
import re

# ---------------------------------------------------------------------------
# MBR / disk constants
# ---------------------------------------------------------------------------
MBR_SIZE = 512
MBR_SIGNATURE_OFFSET = 510
MBR_SIGNATURE = b'\x55\xAA'

MBR_PARTITION_TABLE_OFFSET = 0x1BE   # 446 bytes into MBR
MBR_PARTITION_ENTRY_SIZE   = 16
MBR_NUM_PARTITIONS         = 4

SECTOR_SIZE = 512

# Extra sectors appended after the shrunk filesystem
SAFETY_PADDING_SECTORS = 8192

PARTITION_ENTRY_FMT = '<B3sB3sII'


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def parse_mbr(filepath: str) -> list:
    with open(filepath, 'rb') as fh:
        mbr = fh.read(MBR_SIZE)

    if len(mbr) < MBR_SIZE:
        raise ValueError("File is smaller than one sector - not a disk image.")

    sig = mbr[MBR_SIGNATURE_OFFSET : MBR_SIGNATURE_OFFSET + 2]
    if sig != MBR_SIGNATURE:
        raise ValueError(
            f"Not a valid MBR disk image: expected 0x55AA signature at offset "
            f"0x{MBR_SIGNATURE_OFFSET:X}, got 0x{sig.hex().upper()}."
        )

    partitions = []
    for i in range(MBR_NUM_PARTITIONS):
        off = MBR_PARTITION_TABLE_OFFSET + i * MBR_PARTITION_ENTRY_SIZE
        raw = mbr[off : off + MBR_PARTITION_ENTRY_SIZE]
        status, chs_start, ptype, chs_end, lba_start, lba_size = \
            struct.unpack(PARTITION_ENTRY_FMT, raw)

        if lba_size > 0:
            partitions.append({
                'slot'      : i,
                'mbr_offset': off,
                'status'    : status,
                'chs_start' : chs_start,
                'type'      : ptype,
                'chs_end'   : chs_end,
                'lba_start' : lba_start,
                'lba_size'  : lba_size,
            })

    return partitions


def win_to_wsl_path(win_path: str) -> str:
    path = win_path.replace('\\', '/')
    if len(path) >= 2 and path[1] == ':':
        drive_letter = path[0].lower()
        remainder = path[2:]
        if not remainder.startswith('/'):
            remainder = '/' + remainder
        return f'/mnt/{drive_letter}{remainder}'
    return path


def check_wsl() -> bool:
    try:
        result = subprocess.run(
            ['wsl', 'echo', 'wsl_ok'],
            capture_output=True, text=True, timeout=20
        )
        return 'wsl_ok' in result.stdout
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False


def human_bytes(n: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024.0:
            return f'{n:.2f} {unit}'
        n /= 1024.0
    return f'{n:.2f} PiB'


def _parse_tune2fs_field(output: str, field: str):
    for line in output.splitlines():
        if line.startswith(field + ':'):
            value_str = line.split(':', 1)[1].strip()
            numeric = value_str.split()[0]
            try:
                return int(numeric)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# GUI application
# ---------------------------------------------------------------------------

class PiImageShrinkerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title('Pi Image Shrinker')
        self.resizable(True, True)
        self.minsize(640, 520)
        self._shrink_thread = None
        self._build_ui()

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer = ttk.Frame(self, padding=14)
        outer.grid(row=0, column=0, sticky='nsew')
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(6, weight=1)

        ttk.Label(outer, text='Image file:').grid(
            row=0, column=0, sticky='w', padx=(0, 6), pady=(0, 4))

        self._file_var = tk.StringVar()
        self._file_entry = ttk.Entry(outer, textvariable=self._file_var)
        self._file_entry.grid(row=0, column=1, sticky='ew', padx=(0, 6))

        self._browse_btn = ttk.Button(outer, text='Browse...', command=self._browse)
        self._browse_btn.grid(row=0, column=2)

        self._backup_var = tk.BooleanVar(value=True)
        self._backup_chk = ttk.Checkbutton(
            outer,
            text='Create .bak backup before shrinking (recommended)',
            variable=self._backup_var,
        )
        self._backup_chk.grid(row=1, column=0, columnspan=3, sticky='w', pady=(8, 0))

        self._shrink_btn = ttk.Button(
            outer, text='Shrink Image', command=self._on_shrink_clicked)
        self._shrink_btn.grid(row=2, column=0, columnspan=3, pady=(12, 4))

        self._progress = ttk.Progressbar(outer, mode='indeterminate')
        self._progress.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(0, 8))

        self._status_var = tk.StringVar(value='Ready.')
        ttk.Label(outer, textvariable=self._status_var, anchor='w').grid(
            row=4, column=0, columnspan=3, sticky='ew')

        ttk.Label(outer, text='Log output:', anchor='w').grid(
            row=5, column=0, columnspan=3, sticky='sw', pady=(6, 2))

        self._log = scrolledtext.ScrolledText(
            outer,
            height=16,
            state='disabled',
            wrap='word',
            font=('Consolas', 9),
            background='#1e1e1e',
            foreground='#d4d4d4',
            insertbackground='white',
        )
        self._log.grid(row=6, column=0, columnspan=3, sticky='nsew')

    def _log_append(self, text: str):
        def _update():
            self._log.configure(state='normal')
            self._log.insert('end', text)
            self._log.see('end')
            self._log.configure(state='disabled')
        self.after(0, _update)

    def _log_clear(self):
        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')

    def _set_busy(self, busy: bool):
        def _update():
            state = 'disabled' if busy else 'normal'
            for widget in (self._shrink_btn, self._file_entry,
                           self._browse_btn, self._backup_chk):
                widget.configure(state=state)
            if busy:
                self._progress.start(10)
            else:
                self._progress.stop()
                self._progress['value'] = 0
        self.after(0, _update)

    def _set_status(self, msg: str):
        self.after(0, lambda: self._status_var.set(msg))

    def _browse(self):
        path = filedialog.askopenfilename(
            title='Select Raspberry Pi disk image',
            filetypes=[
                ('Disk images', '*.img *.iso'),
                ('All files',   '*.*'),
            ],
        )
        if path:
            self._file_var.set(path)

    def _on_shrink_clicked(self):
        filepath = self._file_var.get().strip()
        if not filepath:
            messagebox.showerror('No file', 'Please select an .img or .iso file first.')
            return
        if not os.path.isfile(filepath):
            messagebox.showerror('File not found', f'Cannot find:\n{filepath}')
            return
        self._log_clear()
        self._set_busy(True)
        self._set_status('Shrinking...')
        self._shrink_thread = threading.Thread(
            target=self._shrink_worker, args=(filepath,), daemon=True)
        self._shrink_thread.start()

    def _shrink_worker(self, filepath: str):
        try:
            orig_size, final_size = self._do_shrink(filepath)
            saved = orig_size - final_size
            msg = (
                f'Image shrunk successfully!\n\n'
                f'Original : {orig_size:>15,} bytes  ({human_bytes(orig_size)})\n'
                f'Final    : {final_size:>15,} bytes  ({human_bytes(final_size)})\n'
                f'Saved    : {saved:>15,} bytes  ({human_bytes(saved)})\n'
            )
            self._set_status(f'Done - saved {human_bytes(saved)}.')
            self.after(0, lambda: messagebox.showinfo('Shrink Complete', msg))
        except RuntimeError as exc:
            self._log_append(f'\n[ERROR] {exc}\n')
            self._set_status('Failed - see log for details.')
            err_msg = str(exc)
            self.after(0, lambda: messagebox.showerror('Shrink Failed', err_msg))
        except Exception as exc:
            self._log_append(f'\n[UNEXPECTED ERROR] {type(exc).__name__}: {exc}\n')
            self._set_status('Failed (unexpected error).')
            err_msg = f'Unexpected error: {type(exc).__name__}: {exc}'
            self.after(0, lambda: messagebox.showerror('Error', err_msg))
        finally:
            self._set_busy(False)

    def _wsl(self, bash_cmd: str, label: str = None):
        if label:
            self._log_append(f'\n> {label}\n')
        self._log_append(f'  $ {bash_cmd}\n')
        proc = subprocess.Popen(
            ['wsl', 'bash', '-c', bash_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        collected = []
        for line in proc.stdout:
            self._log_append(line)
            collected.append(line)
        proc.wait()
        return proc.returncode, ''.join(collected)

    def _do_shrink(self, filepath: str):
        sep = '-' * 54

        def log(msg=''):
            self._log_append(msg + '\n')

        log(sep)
        log('  Pi Image Shrinker - starting')
        log(sep)

        log()
        log('[1/7]  Checking WSL...')
        if not check_wsl():
            raise RuntimeError(
                'WSL (Windows Subsystem for Linux) is not installed or not responding.\n\n'
                'To install WSL, open PowerShell as Administrator and run:\n'
                '    wsl --install\n\n'
                'Restart your computer after installation, then try again.'
            )
        log('       WSL is available.')

        log()
        log('[2/7]  Parsing MBR partition table...')
        orig_size = os.path.getsize(filepath)
        log(f'       File: {filepath}')
        log(f'       Size: {orig_size:,} bytes ({human_bytes(orig_size)})')

        try:
            partitions = parse_mbr(filepath)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        if not partitions:
            raise RuntimeError('No active partitions found in the MBR partition table.')

        log(f'       Found {len(partitions)} partition(s):')
        for p in partitions:
            size_mib = (p['lba_size'] * SECTOR_SIZE) / (1024 ** 2)
            type_name = {0x0B: 'FAT32', 0x0C: 'FAT32(LBA)', 0x83: 'Linux/ext4'}.get(p['type'], 'Unknown')
            log(f"         Slot {p['slot']+1}: type=0x{p['type']:02X} ({type_name}), "
                f"start={p['lba_start']:>10,}, size={p['lba_size']:>10,} sectors ({size_mib:.1f} MiB)")

        last_part = max(partitions, key=lambda p: p['lba_start'])
        log(f"       Target partition: slot {last_part['slot']+1}, type=0x{last_part['type']:02X}")

        log()
        log('[3/7]  Backup...')
        if self._backup_var.get():
            bak = filepath + '.bak'
            log(f'       Copying to {bak} ...')
            shutil.copy2(filepath, bak)
            log(f'       Backup complete ({human_bytes(os.path.getsize(bak))})')
        else:
            log('       Skipped (checkbox unchecked).')

        log()
        log('[4/7]  Mounting image in WSL...')
        wsl_img = win_to_wsl_path(filepath)
        log(f'       WSL path: {wsl_img}')

        rc, out = self._wsl(
            f'sudo losetup -f --show -P "{wsl_img}"',
            'Attaching loop device',
        )

        loop_dev = out.strip().splitlines()[-1].strip() if out.strip() else ''
        if not re.match(r'^/dev/loop\d+$', loop_dev):
            raise RuntimeError(
                f'losetup did not return a valid loop device path.\nOutput was:\n{out}'
            )

        log(f'       Loop device  : {loop_dev}')
        part_num = last_part['slot'] + 1
        part_dev = f'{loop_dev}p{part_num}'
        log(f'       Partition dev: {part_dev}')

        try:
            log()
            log('[5/7]  Checking filesystem (e2fsck)...')
            rc, out = self._wsl(
                f'sudo e2fsck -f -y "{part_dev}" || true',
                'e2fsck - filesystem check & fix',
            )
            log(f'       e2fsck finished (exit {rc})')

            log()
            log('[6/7]  Shrinking filesystem (resize2fs -M)...')
            rc, out = self._wsl(
                f'sudo resize2fs -M -p "{part_dev}"',
                'resize2fs - shrink ext4 to minimum',
            )
            if rc != 0:
                raise RuntimeError(f'resize2fs exited with code {rc}.\nOutput:\n{out}')
            log('       resize2fs complete')

            rc2, tune_out = self._wsl(
                f'sudo tune2fs -l "{part_dev}"',
                'tune2fs - reading new filesystem parameters',
            )

            block_count = _parse_tune2fs_field(tune_out, 'Block count')
            block_size  = _parse_tune2fs_field(tune_out, 'Block size')

            if block_count is None or block_size is None:
                raise RuntimeError(f'Could not parse Block count/Block size from tune2fs output.')

            log(f'       New block count : {block_count:,}')
            log(f'       Block size      : {block_size} bytes')

        finally:
            log()
            log('       Detaching loop device...')
            self._wsl(f'sudo losetup -d "{loop_dev}"', 'losetup -d')
            log('       Detached.')

        log()
        log('[7/7]  Patching MBR and truncating image...')

        new_fs_bytes  = block_count * block_size
        new_lba_size  = (new_fs_bytes + SECTOR_SIZE - 1) // SECTOR_SIZE
        new_lba_size += SAFETY_PADDING_SECTORS
        new_total_size = (last_part['lba_start'] + new_lba_size) * SECTOR_SIZE

        log(f'       Shrunk FS size   : {new_fs_bytes:,} bytes ({human_bytes(new_fs_bytes)})')
        log(f'       New LBA size     : {new_lba_size:,} sectors (+{SAFETY_PADDING_SECTORS} padding)')
        log(f'       New image size   : {new_total_size:,} bytes ({human_bytes(new_total_size)})')

        with open(filepath, 'r+b') as fh:
            fh.seek(last_part['mbr_offset'])
            raw = fh.read(MBR_PARTITION_ENTRY_SIZE)
            status, chs_start, ptype, chs_end, lba_start, old_lba_size = \
                struct.unpack(PARTITION_ENTRY_FMT, raw)
            new_raw = struct.pack(
                PARTITION_ENTRY_FMT,
                status, chs_start, ptype, chs_end, lba_start, new_lba_size,
            )
            fh.seek(last_part['mbr_offset'])
            fh.write(new_raw)
            log(f'       MBR lba_size updated: {old_lba_size:,} -> {new_lba_size:,}')
            fh.truncate(new_total_size)
            log(f'       File truncated to {new_total_size:,} bytes')

        final_size = os.path.getsize(filepath)
        saved      = orig_size - final_size

        log()
        log(sep)
        log('  DONE')
        log(sep)
        log(f'  Original : {orig_size:>15,} bytes  ({human_bytes(orig_size)})')
        log(f'  Final    : {final_size:>15,} bytes  ({human_bytes(final_size)})')
        log(f'  Saved    : {saved:>15,} bytes  ({human_bytes(saved)})')
        log(sep)

        return orig_size, final_size


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app = PiImageShrinkerApp()
    app.mainloop()
