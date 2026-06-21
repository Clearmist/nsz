#!/usr/bin/env python3
"""Benchmark + correctness test for seekable (chained-dictionary) NCZ compression.

See docs/SeekableCompression.md for the design this exercises.

Usage:
    python test/test_seekable_compression.py /path/to/Game.nsp
    python test/test_seekable_compression.py /path/to/Game.xci

This drives compression/decompression against an user-supplied NSP/XCI.
This is a benchmark/integration test rather than a unit test.

It:

  1. Compresses the input with solid, plain block, and several seekable
     (chained-dictionary block) parameter combinations.
  2. Verifies each output round-trips correctly against the original file
     (nsz.Decompressor.verify, which hash-checks every NCA and the NSP).
  3. For block-based outputs, performs random-offset reads directly through
     the block/seekable decompressor reader and compares them against a
     second, purely sequential reader instance - this is what actually
     proves random seeking is correct, since a full sequential round-trip
     alone doesn't exercise seek()/chain-walking.
  4. Times compression, full decompression, and random-read latency, and
     prints a sorted speed/size table plus a CSV file for further analysis.
"""

import csv
import random
import statistics
import sys
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Allow running this script directly (e.g. `python test/test_seekable_compression.py`)
# without requiring the repo root to already be on sys.path / PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsz.nut import Keys, Print
from nsz.Fs import Nsp, Xci
from nsz import Header, BlockDecompressorReader
from nsz.Decompressor import SeekableDecompressorReader, VerificationException, verify
from nsz.SolidCompressor import solidCompress
from nsz.BlockCompressor import blockCompress
from nsz.SeekableCompressor import seekableCompress

COMPRESSION_LEVEL = 18
USE_LONG_DISTANCE_MODE = False
THREADS = 4

# Parameter grid. Edit these to widen/narrow the sweep - kept modest by
# default so a full run against a real game finishes in a reasonable time.
BLOCK_SIZE_EXPONENTS = [20, 22]  # 1 MiB, 4 MiB
CHAIN_LENGTHS = [4, 16, 64]  # combined with each block size above

RANDOM_READ_COUNT = 64
RANDOM_READ_LENGTH = 64 * 1024
UNCOMPRESSABLE_HEADER_SIZE = 0x4000


def humanSize(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def _findNczFile(container, suffix):
    """nsz/xcz containers expose their files via the default __getitem__-based
    iteration protocol. XCI/XCZ nest an extra level: top-level HFS0
    partitions (we only care about "secure", the one block/seekable
    compression actually targets), each containing the real NCA/NCZ files."""
    if suffix == ".xcz" or suffix == ".xci":
        secure = None
        for partition in container.hfs0:
            if partition._path == "secure":
                secure = partition
                break
        if secure is None:
            return None
        candidates = secure
    else:
        candidates = container
    for nspf in candidates:
        if nspf._path.endswith(".ncz"):
            return nspf
    return None


def openBlockReader(nszPath):
    """Open a fresh container handle and return (container, reader, blockHeader)
    for the first .ncz entry found, or None if the entry isn't block-based
    (e.g. solid mode has no NCZBLOCK header at all). Caller must close the
    returned container when done with the reader."""
    suffix = Path(nszPath).suffix.lower()
    container = Nsp.Nsp() if suffix == ".nsz" else Xci.Xci()
    container.open(str(nszPath), "rb")
    nczFile = _findNczFile(container, suffix)
    if nczFile is None:
        container.close()
        return None
    nczFile.seek(UNCOMPRESSABLE_HEADER_SIZE)
    magic = nczFile.read(8)
    if magic != b"NCZSECTN":
        container.close()
        raise RuntimeError(f"{nszPath}: ncz entry has no NCZSECTN header")
    sectionCount = nczFile.readInt64()
    for _ in range(sectionCount):
        Header.Section(nczFile)
    pos = nczFile.tell()
    blockMagic = nczFile.read(8)
    nczFile.seek(pos)
    if blockMagic != b"NCZBLOCK":
        container.close()
        return None
    blockHeader = Header.Block(nczFile)
    if blockHeader.type == 2:
        reader = SeekableDecompressorReader(nczFile, blockHeader)
    else:
        reader = BlockDecompressorReader.BlockDecompressorReader(nczFile, blockHeader)
    return container, reader, blockHeader


def randomAccessCheck(nszPath):
    """Returns (correct, avgReadMs, maxReadMs). (True, 0.0, 0.0) if there's
    no block-based NCA inside (e.g. solid mode) to check."""
    opened = openBlockReader(nszPath)
    if opened is None:
        return True, 0.0, 0.0
    container, reader, blockHeader = opened
    opened2 = openBlockReader(nszPath)
    if opened2 is None:
        container.close()
        return True, 0.0, 0.0
    container2, refReader, _ = opened2
    try:
        size = blockHeader.decompressedSize
        if size <= RANDOM_READ_LENGTH:
            return True, 0.0, 0.0
        rng = random.Random(1234)
        offsets = [
            rng.randrange(0, size - RANDOM_READ_LENGTH)
            for _ in range(RANDOM_READ_COUNT)
        ]
        timings = []
        correct = True
        for offset in offsets:
            start = time.perf_counter()
            reader.seek(offset)
            got = reader.read(RANDOM_READ_LENGTH)
            timings.append(time.perf_counter() - start)

            refReader.seek(offset)
            expected = refReader.read(RANDOM_READ_LENGTH)
            if got != expected:
                correct = False
        return correct, statistics.mean(timings) * 1000, max(timings) * 1000
    finally:
        container.close()
        container2.close()


def correctnessCheck(nszPath, inputPath):
    try:
        verify(nszPath, False, True, True, inputPath)
        return True, ""
    except VerificationException as e:
        return False, str(e)
    except BaseException as e:
        return False, repr(e)


def runConfig(label, compressFn, inputPath, workDir):
    outputDir = workDir / label
    outputDir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== {label} ===")

    start = time.perf_counter()
    nszPath = compressFn(outputDir)
    compressTime = time.perf_counter() - start
    assert nszPath is not None and Path(nszPath).is_file(), (
        f"{label} produced no output file"
    )

    inputSize = inputPath.stat().st_size
    outputSize = Path(nszPath).stat().st_size

    start = time.perf_counter()
    hashCorrect, hashError = correctnessCheck(nszPath, inputPath)
    decompressTime = time.perf_counter() - start

    # Run independently of hashCorrect: this is what actually proves
    # seek()/chain-walking is correct, and a hash mismatch (e.g. from stale
    # keys.txt) shouldn't hide a regression here or vice versa.
    avgReadMs = maxReadMs = 0.0
    try:
        randomCorrect, avgReadMs, maxReadMs = randomAccessCheck(nszPath)
        randomError = (
            "" if randomCorrect else "random-access read mismatch vs sequential read"
        )
    except BaseException as e:
        randomCorrect = False
        randomError = f"random-access check raised: {e!r}"

    correct = hashCorrect and randomCorrect
    error = " | ".join(e for e in (hashError, randomError) if e)

    if not correct:
        print(
            f"[FAIL] {label}: hashCorrect={hashCorrect} randomAccessCorrect={randomCorrect}: {error}"
        )
    else:
        print(
            f"[PASS] {label}: {humanSize(outputSize)} "
            f"({100 * outputSize / inputSize:.1f}%), "
            f"compress {compressTime:.1f}s, verify {decompressTime:.1f}s"
        )

    return {
        "label": label,
        "inputSize": inputSize,
        "outputSize": outputSize,
        "ratioPct": 100 * outputSize / inputSize,
        "compressTimeS": compressTime,
        "decompressTimeS": decompressTime,
        "avgRandomReadMs": avgReadMs,
        "maxRandomReadMs": maxReadMs,
        "hashCorrect": hashCorrect,
        "randomAccessCorrect": randomCorrect,
        "correct": correct,
        "error": error,
    }


def printTable(rows):
    headers = [
        "label",
        "size",
        "ratio%",
        "compress(s)",
        "verify(s)",
        "avg seek(ms)",
        "max seek(ms)",
        "hash",
        "seek",
    ]
    widths = [16, 10, 8, 12, 10, 13, 13, 6, 6]
    print()
    print(" | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in sorted(rows, key=lambda r: r["outputSize"]):
        cols = [
            r["label"],
            humanSize(r["outputSize"]),
            f"{r['ratioPct']:.2f}",
            f"{r['compressTimeS']:.1f}",
            f"{r['decompressTimeS']:.1f}",
            f"{r['avgRandomReadMs']:.2f}",
            f"{r['maxRandomReadMs']:.2f}",
            "PASS" if r["hashCorrect"] else "FAIL",
            "PASS" if r["randomAccessCorrect"] else "FAIL",
        ]
        print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))


def writeMarkdown(rows, inputPath, path):
    headers = [
        "label",
        "size",
        "ratio%",
        "compress(s)",
        "verify(s)",
        "avg seek(ms)",
        "max seek(ms)",
        "hash",
        "seek",
    ]
    lines = [
        "# Seekable compression benchmark",
        "",
        f"- Input: `{inputPath}` ({humanSize(inputPath.stat().st_size)})",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for r in sorted(rows, key=lambda r: r["outputSize"]):
        cols = [
            r["label"],
            humanSize(r["outputSize"]),
            f"{r['ratioPct']:.2f}",
            f"{r['compressTimeS']:.1f}",
            f"{r['decompressTimeS']:.1f}",
            f"{r['avgRandomReadMs']:.2f}",
            f"{r['maxRandomReadMs']:.2f}",
            "PASS" if r["hashCorrect"] else "FAIL",
            "PASS" if r["randomAccessCorrect"] else "FAIL",
        ]
        lines.append("| " + " | ".join(cols) + " |")

    failed = [r for r in rows if not r["correct"]]
    lines.append("")
    if failed:
        lines.append(f"**{len(failed)} configuration(s) FAILED correctness checks:**")
        lines.append("")
        for r in failed:
            lines.append(f"- `{r['label']}`: {r['error']}")
    else:
        lines.append("All configurations passed correctness checks.")

    path.write_text("\n".join(lines) + "\n")
    print(f"Markdown report written to {path}")


def writeCsv(rows, path):
    fieldnames = [
        "label",
        "inputSize",
        "outputSize",
        "ratioPct",
        "compressTimeS",
        "decompressTimeS",
        "avgRandomReadMs",
        "maxRandomReadMs",
        "hashCorrect",
        "randomAccessCorrect",
        "correct",
        "error",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    print(f"\nCSV written to {path}")


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <uncompressed .nsp or .xci file>")
        sys.exit(1)

    inputPath = Path(sys.argv[1]).resolve()
    if not inputPath.is_file():
        print(f"{inputPath} is not a file")
        sys.exit(1)
    if inputPath.suffix.lower() not in (".nsp", ".xci"):
        print("This benchmark only supports .nsp/.xci input files.")
        sys.exit(1)

    Print.machineReadableOutput = False
    Keys.load_default()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    workDir = Path(tempfile.mkdtemp(prefix="nsz_seekable_bench_"))
    print(f"Input:             {inputPath} ({humanSize(inputPath.stat().st_size)})")
    print(f"Working directory: {workDir}")

    rows = []
    try:
        rows.append(
            runConfig(
                "solid",
                lambda outDir: solidCompress(
                    inputPath,
                    COMPRESSION_LEVEL,
                    False,
                    False,
                    USE_LONG_DISTANCE_MODE,
                    outDir,
                    THREADS,
                    {},
                    0,
                    None,
                ),
                inputPath,
                workDir,
            )
        )

        for blockSizeExponent in BLOCK_SIZE_EXPONENTS:
            rows.append(
                runConfig(
                    f"block bs={blockSizeExponent}",
                    lambda outDir, bs=blockSizeExponent: blockCompress(
                        inputPath,
                        COMPRESSION_LEVEL,
                        False,
                        False,
                        USE_LONG_DISTANCE_MODE,
                        bs,
                        outDir,
                        THREADS,
                    ),
                    inputPath,
                    workDir,
                )
            )
            for chainLength in CHAIN_LENGTHS:
                rows.append(
                    runConfig(
                        f"chain bs={blockSizeExponent} chain={chainLength}",
                        lambda outDir, bs=blockSizeExponent, cl=chainLength: (
                            seekableCompress(
                                inputPath,
                                COMPRESSION_LEVEL,
                                False,
                                False,
                                USE_LONG_DISTANCE_MODE,
                                bs,
                                cl,
                                outDir,
                                THREADS,
                            )
                        ),
                        inputPath,
                        workDir,
                    )
                )
    finally:
        printTable(rows)
        writeCsv(rows, Path.cwd() / "seekable_compression_benchmark.csv")
        writeMarkdown(
            rows,
            inputPath,
            Path.cwd() / f"seekable_compression_benchmark_{timestamp}.md",
        )
        shutil.rmtree(workDir, ignore_errors=True)

    failed = [r for r in rows if not r["correct"]]
    if failed:
        print(f"\n{len(failed)} configuration(s) FAILED correctness checks:")
        for r in failed:
            print(f"  - {r['label']}: {r['error']}")
        sys.exit(1)

    print("\nAll configurations passed correctness checks.")


if __name__ == "__main__":
    main()
