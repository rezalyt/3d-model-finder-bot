#!/usr/bin/env python3
import os
import sys

REAL = "/usr/local/bin/TotalSegmentator-real"


def main():
    raw = sys.argv[1:]
    clean = []
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg == "--force_split":
            i += 1
            continue
        if arg in ("--nr_thr_resamp", "--nr_thr_saving"):
            i += 2
            continue
        clean.append(arg)
        i += 1
    cmd = [REAL, *clean, "--force_split", "--nr_thr_resamp", "1", "--nr_thr_saving", "1"]
    if "--fast" not in clean and "--fastest" not in clean:
        cmd.append("--fast")
    os.execv(REAL, cmd)


if __name__ == "__main__":
    main()
