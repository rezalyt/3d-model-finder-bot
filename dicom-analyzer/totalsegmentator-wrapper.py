#!/usr/bin/env python3
import os
import sys

REAL = "/usr/local/bin/TotalSegmentator-real"

def main():
    args = sys.argv[1:]
    # Keep the existing invocation intact, but force low-memory options for Railway CPU.
    extra = ["--force_split", "--nr_thr_resamp", "1", "--nr_thr_saving", "1"]
    if "--fast" not in args and "--fastest" not in args:
        extra += ["--fast"]
    # Avoid duplicate flags if the caller already supplies them.
    for flag in ("--force_split", "--nr_thr_resamp", "--nr_thr_saving"):
        while flag in args:
            i = args.index(flag)
            del args[i:i+2] if flag != "--force_split" else args[i:i+1]
    cmd = [REAL, *args, *extra]
    os.execv(REAL, cmd)

if __name__ == "__main__":
    main()
