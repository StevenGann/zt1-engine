"""Make the Linux build: the engine as a folder that runs on its own.

Run by the build workflow (.github/workflows/build.yml), or by hand:
    python engine-build-resources/make_linux_release.py [--version 2026.10.19] [--no-build]

1. Builds the engine with CMake and Ninja, Release, in build-linux/ (a folder
   of its own: the Windows build's build-x64/ and the Lab's build/ are left
   alone), with the version baked in. SDL, SDL_image, SDL_mixer and SDL_ttf
   are built from the vendored copies and linked in statically, so the result
   carries no SDL of its own; SDL finds X11 or Wayland at runtime by itself.
2. Packs dist/ZT1-Engine-<version>-linux-x86_64.tar.gz and its .sha256: the
   binary, the fonts, the licence, the release notes, and the shared libraries
   the binary needs that a desktop Linux is not certain to have already -
   libzip, and the compression and crypto libraries libzip itself wants -
   beside a run.sh that points the loader at them.

The fonts matter more here than they look: the engine reads them from beside
itself, and without them it starts, loads everything, and draws no text at all
(no buttons, no panels, no prices) with nothing in the log to say why. They go
in the folder, and the build copies them beside the binary.

The version is a date, year.month.day without leading zeros (2026.10.19),
the same as the Windows release.

No Zoo Tycoon content goes in: the folder reads the player's own install,
named by ZT1_GAME_DIR, or found by being started from the game's folder.
"""
import argparse
import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build-linux")
EXE = os.path.join(BUILD, "zt1-engine")
DIST = os.path.join(ROOT, "dist")
# The original game's DLLs (never shipped)
GAME_DLL_PREFIXES = ("lang", "res", "dwin", "ebueula", "eula", "imeuires", "ijl15", "setupenu")
# What the loader and a desktop Linux are certain to have. Everything else the
# binary names at link time is ours to carry. (X11 and Wayland are not in this
# list because they are not linked: SDL opens them at runtime, so `ldd` never
# mentions them, and every desktop has them.)
SYSTEM_LIBS = (
    "linux-vdso", "ld-linux", "libc.so", "libm.so", "libdl.so", "libpthread.so",
    "librt.so", "libstdc++.so", "libgcc_s.so", "libEGL", "libGL", "libvulkan",
)
RUN_SH = """#!/bin/sh
# Run the engine. Name your Zoo Tycoon folder with ZT1_GAME_DIR, or pass it as
# the first argument; if you put this folder inside the game's own folder, it
# will be found by itself.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "$1" ] && [ -d "$1" ]; then ZT1_GAME_DIR="$1"; shift; fi
if [ -z "$ZT1_GAME_DIR" ]; then
    for d in "$here" "$here/.." "$here/../.."; do
        if [ -f "$d/animals.ztd" ] || [ -f "$d/Animals.ztd" ]; then
            ZT1_GAME_DIR="$d"; break
        fi
    done
fi
if [ -z "$ZT1_GAME_DIR" ]; then
    echo "Where is Zoo Tycoon? Set ZT1_GAME_DIR and run this again:" >&2
    echo "    ZT1_GAME_DIR=\\"/path/to/Zoo Tycoon\\" $0" >&2
    echo "(or put this folder inside the game's own folder)" >&2
    exit 1
fi
export ZT1_GAME_DIR
LD_LIBRARY_PATH="$here/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "$here/zt1-engine" "$@"
"""


def say(*a):
    print(*a, flush=True)


def find(name, candidates=()):
    found = shutil.which(name) or next((p for p in candidates if p and os.path.exists(p)), None)
    if not found:
        sys.exit(f"[X] No {name} found")
    return found


def run(args):
    say("> " + " ".join(f'"{a}"' if " " in a else a for a in args))
    p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    for line in p.stdout:
        say(line.rstrip())
    if p.wait() != 0:
        sys.exit(f"[X] Failed ({p.returncode}): {os.path.basename(args[0])}")


def build(version, extra=()):
    cmake = find("cmake")
    say(f"[1/2] Building zt1-engine {version}, x86_64 Release (build-linux/)")
    # The vendored SDL_mixer and SDL_ttf, so this links no SDL of the system's
    # and the folder carries none: what the binary needs at run time is then
    # the C++ runtime, libzip, and nothing else we did not put beside it.
    # (extra: for a machine whose headers are not where a distribution puts
    #  them - one built without root, say. A normal build passes none.)
    run([cmake, "-S", ROOT, "-B", BUILD, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DSDL2MIXER_VENDORED=ON", "-DSDL2TTF_VENDORED=ON", f"-DZT1_VERSION={version}"]
        + list(extra))
    run([cmake, "--build", BUILD, "--parallel"])
    if not os.path.exists(EXE):
        sys.exit("[X] The build said it worked, but there's no " + os.path.relpath(EXE, ROOT))


def needed_libraries():
    """What the binary links that a desktop does not certainly have."""
    ldd = find("ldd")
    out = subprocess.run([ldd, EXE], capture_output=True, text=True).stdout
    libs = []
    for line in out.splitlines():
        line = line.strip()
        if "=>" not in line:
            continue
        path = line.split("=>", 1)[1].strip().split(" ")[0]
        if not path.startswith("/") or any(s in os.path.basename(path) for s in SYSTEM_LIBS):
            continue
        libs.append(path)
    return libs


def game_build(version):
    say("[2/2] The folder that runs on its own (tar.gz)")
    name = f"ZT1-Engine-{version}-linux-x86_64"
    out = os.path.join(DIST, name + ".tar.gz")
    entries = [(f"{name}/zt1-engine", EXE)]
    for f in sorted(os.listdir(os.path.join(ROOT, "fonts"))):
        if f.lower().endswith(".otf"):
            entries.append((f"{name}/fonts/{f}", os.path.join(ROOT, "fonts", f)))
    for f in ("LICENSE.txt", "THIRD_PARTY_LICENSES.md"):
        entries.append((f"{name}/{f}", os.path.join(ROOT, f)))
    notes = os.path.join(ROOT, "engine-build-resources", "RELEASE_README.md")
    entries.append((f"{name}/README.md", notes))
    entries.append((f"{name}/README.txt", notes))
    libs = needed_libraries()
    for path in libs:
        # Take the file the linker name points at, not the link: a
        # distribution's libzip.so.5 is a symlink to libzip.so.5.5, and
        # packing the link ships a link to a file that is not there (the
        # loader reports "no such file" and quietly uses the system's copy
        # instead - or fails, where there is no system copy to fall back on).
        # The name stays the one the loader asks for, which is the soname.
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            sys.exit(f"[X] Carried library does not resolve to a file: {path} -> {real}")
        entries.append((f"{name}/lib/{os.path.basename(path)}", real))
    # The guard: nothing of the game's slips in (the same rule as the Windows
    # Game Build, and the reason the licences are the only third-party files)
    bad = [a for a, _ in entries
           if a.lower().endswith((".ztd", ".zoo"))
           or (a.lower().endswith(".dll") and os.path.basename(a).lower().startswith(GAME_DLL_PREFIXES))]
    if bad:
        sys.exit("[X] Refusing to package the game's files: " + ", ".join(bad[:5]))
    missing = [src for _a, src in entries if not os.path.isfile(src)]
    if missing:
        sys.exit("[X] Missing: " + ", ".join(os.path.relpath(m, ROOT) for m in missing))
    os.makedirs(DIST, exist_ok=True)
    tmp = out + ".tmp"
    with tarfile.open(tmp, "w:gz") as t:
        for arc, src in entries:
            ti = t.gettarinfo(src, arcname=arc)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mtime = 0
            # The binary and the launcher have to arrive executable: a tar that
            # loses the bit is the same as not shipping them.
            ti.mode = 0o755 if arc.endswith(("zt1-engine", "run.sh")) else 0o644
            with open(src, "rb") as fh:
                t.addfile(ti, fh)
        # The launcher, written here rather than read from the tree (nothing
        # in the repo needs it, and one copy cannot drift from another)
        ti = tarfile.TarInfo(f"{name}/run.sh")
        data = RUN_SH.encode()
        ti.size, ti.mode, ti.mtime = len(data), 0o755, 0
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        import io
        t.addfile(ti, io.BytesIO(data))
        # Folders the game writes to, so a first run does not start by
        # inventing them
        for d in ("saves/", "mods/"):
            ti = tarfile.TarInfo(f"{name}/{d}")
            ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            t.addfile(ti)
    os.replace(tmp, out)
    sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w", newline="\n") as fh:
        fh.write(f"{sha}  {os.path.basename(out)}\n")
    say(f"[OK] {os.path.relpath(out, ROOT)}: {len(entries) + 1} files, {os.path.getsize(out) / 1024 / 1024:.1f} MB")
    say(f"     carried libraries: {', '.join(os.path.basename(p) for p in libs) or 'none needed'}")
    say(f"     sha256 {sha}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="", help="year.month.day, e.g. 2026.10.19 (else today's)")
    ap.add_argument("--no-build", action="store_true", help="pack the binary already in build-linux/")
    ap.add_argument("--cmake-arg", action="append", default=[], metavar="-DNAME=VALUE",
                    help="an extra argument for cmake (a machine whose headers are not where a "
                         "distribution puts them, say); repeatable, usually none")
    a = ap.parse_args()
    today = datetime.date.today()
    version = a.version or f"{today.year}.{today.month}.{today.day}"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"[X] The version is three numbers, like 2026.10.19 (not {version})")
    if not a.no_build:
        build(version, a.cmake_arg)
    elif not os.path.exists(EXE):
        sys.exit("[X] No " + os.path.relpath(EXE, ROOT) + ": build first")
    game_build(version)
