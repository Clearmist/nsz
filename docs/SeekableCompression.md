# Seekable Compression: Implementation Plan

## Problem

NSZ has two compression modes today:

- **Solid** ([SolidCompressor.py](../nsz/SolidCompressor.py)): the whole NCA payload is fed
  into a single `ZstdCompressor.stream_writer()` as one continuous zstd frame. Best ratio,
  but reading byte N requires decompressing bytes `0..N` first - no random access.
- **Block** ([BlockCompressor.py](../nsz/BlockCompressor.py)): the payload is split into
  fixed-size blocks, each compressed as its own independent zstd frame, indexed by an
  `NCZBLOCK` header ([Header.py](../nsz/Header.py)). [BlockDecompressorReader.py](../nsz/BlockDecompressorReader.py)
  can seek straight to any block. Fully random access, but each block loses the cross-block
  redundancy solid mode exploits, costing several percent of compression ratio.

Solid's ratio advantage and block's seekability are in direct tension: zstd frames cannot be
entered mid-stream without replaying the decoder state from the start of the frame. True
solid compression and true O(1) random access are mutually exclusive in zstd's frame model.

## Approach: chained-dictionary blocks

The practical middle ground is **dictionary-chained blocks**, a new `NCZBLOCK` type (`type=2`)
alongside the existing independent-block type (`type=1`):

- Data is still split into fixed-size blocks (`blockSizeExponent`, same as today).
- Blocks are grouped into chains of `chainLength` blocks.
- The first block of each chain ("sync block") is compressed independently, exactly like
  block mode today.
- Every other block in the chain is compressed using the **raw (decompressed) bytes of the
  immediately preceding block** as a zstd content-only dictionary
  (`ZstdCompressionDict(..., dict_type=DICT_TYPE_RAWCONTENT)`), recovering most of the
  cross-block redundancy solid mode benefits from.

This gives a single tunable knob, `chainLength`, that trades ratio against worst-case seek
cost:

- `chainLength = 1` degenerates to today's independent block mode (no dictionary ever used).
- `chainLength = numberOfBlocks` degenerates to effectively solid (one chain, no seek benefit).
- Intermediate values (e.g. 4-64) approximate solid's ratio while bounding the cost of a
  random seek to at most `chainLength - 1` extra block decompressions (to walk forward from
  the nearest preceding sync block to the target block).

Compression stays fully parallel: building block `i`'s dictionary only needs the *raw* bytes
of block `i-1`, which the main process already has in hand from reading the source
sequentially - it does not need block `i-1`'s *compressed* output, so worker processes are
still independent and the existing multiprocessing pool in `BlockCompressor.py` can be reused
almost as-is.

Decompression's worst case is now sequential within a chain: to read block `i`, the decoder
must decompress every block from the start of `i`'s chain up to and including `i`, because
each block's content dictionary is the previous block's plaintext. The decompressor reader
caches the last-decompressed block, so sequential reads (the common case during a normal
full decompression) are not affected.

## File format changes

`NCZBLOCK` header layout is unchanged byte-for-byte; only the meaning of two existing fields
changes:

```
Magic:                  8 bytes  (b"NCZBLOCK")
Version:                1 byte
Type:                   1 byte   1 = independent blocks, 2 = chained-dictionary blocks (NEW)
3rd byte:               1 byte   unused for type 1; chainLength (1-255) for type 2 (NEW meaning)
Block Size Exponent:    1 byte
Number of Blocks:       4 bytes
Decompressed Size:      8 bytes
Compressed Block Sizes: N x 4 bytes
```

No new fields, no version bump needed for the container format itself. [Header.py](../nsz/Header.py)
`Block.__init__` now validates `type` against `Block.SUPPORTED_TYPES` and rejects unknown
types with a clear error ("Please update nsz...") instead of silently producing wrong bytes
or an opaque zstd error - this validation did not previously exist for `type`/`version` at
all, which was a latent forward-compatibility gap.

A reader that does not understand type 2 and used the old `BlockDecompressorReader`
unconditionally would attempt to decompress dictionary-dependent frames without ever
supplying a dictionary; zstd raises a clear decoding error in that case (it cannot resolve
back-references into data it never saw), so old binaries fail loudly rather than emitting
silently corrupted output - but with the new explicit type check in `Header.Block`, they fail
even earlier with a readable message.

## Code layout

- **`nsz/SeekableCompressor.py`** (new): mirrors `BlockCompressor.py`'s structure
  (`seekableCompress` -> `seekableCompressNsp`/`seekableCompressXci` ->
  `seekableCompressContainer`, plus a `compressBlockTask` worker function for the
  multiprocessing pool). Differences from `BlockCompressor.py`:
  - Takes an additional `chainLength` parameter.
  - Tracks the previous block's raw bytes while reading sequentially from the source
    partitions, resetting to `None` at the start of every chain.
  - Passes `(buffer, compressionLevel, useLongDistanceMode, chunkRelativeBlockID, dictBytes)`
    to workers; workers build a `ZstdCompressionDict` from `dictBytes` when present.
  - Writes the `NCZBLOCK` header with `type=2` and `chainLength` in the 3rd byte.
- **`nsz/Decompressor.py`**: adds a `SeekableDecompressorReader` class implementing the same
  `seek(offset, whence)` / `read(length)` interface as `BlockDecompressorReader`, plus chain
  walking (decompress forward from the nearest preceding sync block, caching the most
  recently decompressed block for dictionary reuse). `__decompressNcz` dispatches to it when
  `Header.Block.type == 2`, and to the existing `BlockDecompressorReader` when `type == 1`;
  no other code in `__decompressNcz` needs to change since both readers share an interface.
- **`nsz/Header.py`**: `Block` class changes described above.
- **CLI** (`ParseArguments.py`, `__init__.py`): new `--chain N` option (default `1`, i.e. no
  behavior change unless requested). `compress()` calls `seekableCompress` instead of
  `blockCompress` when `--block` is combined with `--chain` > 1.

## Testing strategy

Implemented in [`test/test_seekable_compression.py`](../test/test_seekable_compression.py).
Run against a real game dump supplied by the user (NSZ cannot ship copyrighted fixtures), as a
single positional argument:

```
python test/test_seekable_compression.py /path/to/Game.nsp
```

1. **Correctness (must-pass)**: for every parameter combination, compress then fully
   decompress and compare the SHA-256 of the result against the original file (reusing
   `Decompressor.verify`'s existing hash-check machinery). Also performs targeted random-range
   reads through `SeekableDecompressorReader`/`BlockDecompressorReader` directly and compares
   the bytes against a full sequential decompression of the same byte range - this is what
   actually exercises the new seek/chain-walk logic, since a correct full round-trip alone
   does not prove random access works.
2. **Ratio regression**: compresses the same input with solid, plain block (`chainLength=1`),
   and several `chainLength` values, recording output size and ratio relative to solid, so a
   regression (chained mode landing further from solid than expected) is visible directly in
   the table rather than requiring manual comparison.
3. **Performance / speed-size table**: for the full `blockSizeExponent` x `chainLength` x
   `compressionLevel` grid, records compress time, full decompress time, and output size, and
   prints a sorted markdown table - this is the table used to pick good defaults.
4. **Random-seek latency**: for each chained configuration, performs N random-offset reads of
   a fixed size and reports average/worst latency, compared against the `chainLength=1`
   baseline, to quantify the actual cost of the seek/chain-walk tradeoff at each chain length.
