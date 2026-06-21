# Quickstart: Reading Dictionary-Chained Blocks (NCZBLOCK type 2)

This is an integration guide for emulators, file managers, or any third-party
application that wants to randomly read (e.g. mount/stream) `.ncz`/`.nsz`/`.xcz`
files produced with nsz's seekable chained-dictionary block mode, without
fully decompressing the file first.

If you already support nsz's plain block mode (`NCZBLOCK` type 1, documented
in the main [README.md](../README.md#ncz)), this is a small addition: the
block index format is identical, decompressing any individual block is
identical, and the only new thing is a short "walk back and replay" step
before you can decompress an arbitrary block. If you're starting from
scratch, read this instead of (or before) the type 1 docs - the type 2 reader
described here is a strict superset (set `chainLength = 1` and it behaves
exactly like type 1).

See [SeekableCompression.md](SeekableCompression.md) for the design
rationale. This document is the "how do I implement a reader" companion.

## 1. Detect the mode

```
nspf.seek(0x4000)
magic = nspf.read(8)
if magic == b"NCZSECTN":
    # parse section list (needed regardless of mode - see README.md)
    ...
    pos = nspf.tell()
    blockMagic = nspf.peek(8)   # read, then seek back
    if blockMagic == b"NCZBLOCK":
        header = parse_block_header(nspf)   # see below
        if header.type == 1:
            # plain independent blocks - O(1) seek
        elif header.type == 2:
            # dictionary-chained blocks - this document
        else:
            # unknown type: reject the file with a clear error instead of
            # guessing. nsz itself does this in Header.Block.
    else:
        # solid mode: one continuous zstd frame, no random access possible.
```

**Always check `type` explicitly and reject unknown values.** The 3rd header
byte (`chainLength` for type 2) has no meaning for type 1, and a future type
3+ might repurpose other fields too. Treating an unrecognized type as type 1
will not crash safely - it will hand zstd compressed bytes that depend on a
dictionary it was never given, which fails decoding, but failing loudly with
a clear "unsupported NCZBLOCK type" error before that point is much easier
to debug.

## 2. Header layout (byte-identical to type 1)

```
Offset  Size  Field
0x00    8     Magic: b"NCZBLOCK"
0x08    1     Version
0x09    1     Type: 1 = independent blocks, 2 = chained-dictionary blocks
0x0A    1     Unused (type 1)  /  chainLength, 1-255 (type 2)
0x0B    1     blockSizeExponent (block size = 2^x, x in [14, 32])
0x0C    4     numberOfBlocks (uint32, little-endian)
0x10    8     decompressedSize (uint64, little-endian)
0x18    4*N   compressedBlockSizeList: N = numberOfBlocks uint32s
```

Block byte offsets in the file are derived exactly as in type 1: the first
block starts immediately after this header, and block `i`'s offset is the
sum of all `compressedBlockSizeList[0..i-1]`.

The decompressed size of block `i` is `blockSize` for every block except the
last, which is `decompressedSize % blockSize` (or `blockSize` if that's
zero).

A block is **stored raw** (not zstd-compressed at all) when
`compressedBlockSizeList[i] >= decompressedBlockSize(i)`; read it directly
instead of decompressing. This rule is identical for type 1 and type 2 - it
only ever applies to the bytes of block `i` itself, never affects whether
block `i` was used as someone else's dictionary, and a raw-stored block can
still be the dictionary source for the next block in its chain.

## 3. The chaining rule

Blocks are grouped into chains of `chainLength` consecutive blocks. Within
each chain:

- The **first block** (`blockID % chainLength == 0`, the "sync block") is a
  normal, independent zstd frame - decompress it with no dictionary, exactly
  like type 1.
- **Every other block** in the chain was compressed using the *raw,
  decompressed* bytes of the immediately preceding block (`blockID - 1`) as
  a zstd **raw content dictionary** (not a trained/formatted zstd dictionary
  - just the literal previous block's plaintext, used directly as the
  dictionary buffer).

So to decompress block N, you need the raw plaintext of block N-1, which
needs N-2, and so on back to the most recent sync block. In the worst case
that's `chainLength - 1` extra block decompressions - bounded, unlike solid
mode's "decompress from the start of the file" cost, but not the O(1) of
type 1.

### Algorithm: decompress block N

```python
def decompress_block(blockID):
    if blockID == cache.id:
        return cache.raw                      # already have it

    chainPos = blockID % chainLength
    if chainPos != 0 and cache.id != blockID - 1:
        # Don't have the immediately preceding block cached - walk back to
        # this chain's sync block and replay forward so we do.
        chainStart = blockID - chainPos
        for prev in range(chainStart, blockID):
            decompress_block(prev)            # fills cache as a side effect

    compressed = read_compressed_bytes(blockID)
    decompressedSize = decompressed_block_size(blockID)

    if len(compressed) >= decompressedSize:
        raw = compressed                      # stored raw, see section 2
    elif chainPos == 0:
        raw = zstd_decompress(compressed, max_output_size=decompressedSize)
    else:
        # cache.raw is now guaranteed to be blockID - 1's raw bytes
        raw = zstd_decompress_with_dict(
            compressed, dict=cache.raw, max_output_size=decompressedSize
        )

    cache.id, cache.raw = blockID, raw
    return raw
```

Key implementation notes:

- **One single-slot cache is enough.** You don't need to retain every block
  in a chain, only the most recently decompressed one - it's always either
  the answer you want, or the dictionary for the next step of the walk-back.
- **Sequential reads are cheap.** If your access pattern reads block N then
  N+1 then N+2 (the common case for full decompression or streaming
  playback), the cache hit on `cache.id == blockID - 1` means you never walk
  back; each block costs exactly one decompression, identical to type 1.
- **Random reads cost at most `chainLength` decompressions**, not the whole
  file. This is the entire point of this mode versus solid compression.
- **`max_output_size` is required**, not optional, when supplying a
  dictionary - zstd cannot otherwise determine the frame's decompressed size
  up front for dictionary-using frames. Without it you'll see an error like
  "error determining content size from frame header" even on correctly
  formed input.

### Reference implementations

- Python (the canonical implementation, used by nsz itself):
  [`nsz/Decompressor.py`](../nsz/Decompressor.py) - class
  `SeekableDecompressorReader`. Uses `zstandard.ZstdCompressionDict(...,
  dict_type=DICT_TYPE_RAWCONTENT)` for the dictionary and
  `ZstdDecompressor(dict_data=...).decompress(data,
  max_output_size=...)` to decode.
- The writer side, if you need to produce these files too, or just want to
  see how the dictionaries are built during compression:
  [`nsz/SeekableCompressor.py`](../nsz/SeekableCompressor.py).
- C/C++: the equivalent libzstd calls are `ZSTD_decompress_usingDict()` (or
  `ZSTD_DCtx_loadDictionary()` + `ZSTD_decompressDCtx()` on a reusable
  `ZSTD_DCtx`) with `dictBuffer`/`dictSize` set to the previous block's raw
  bytes, and `ZSTD_getFrameContentSize()`/a known buffer size standing in
  for `max_output_size`.

## 4. Re-encryption (only if you need byte-identical NCA output)

Everything above gets you the **decrypted** NCA payload bytes. If your use
case only needs filesystem contents (e.g. an emulator reading game data
directly), you can stop here. If you need to reproduce the exact original
(encrypted) NCA bytes - e.g. to write out a `.nca` file - re-apply AES-128-CTR
per the `NCZSECTN` section list (`cryptoType`, `cryptoKey`, `cryptoCounter`)
the same way [`Decompressor.py`](../nsz/Decompressor.py)'s `__decompressNcz`
does, using each section's byte range to know which key/counter applies to
which part of the decompressed stream.

## 5. Testing your implementation

You don't need original game files to sanity-check the core chaining logic -
it's pure zstd plus arithmetic, no encryption involved. The cheapest test is
the one nsz's own benchmark uses
([`test/test_seekable_compression.py`](../test/test_seekable_compression.py),
function `randomAccessCheck`): decompress the same byte range two ways -
once via a purely sequential pass from block 0, once by seeking directly to
it - and assert the bytes are identical. If your reader passes that for
offsets scattered across multiple chains (not just within the first one),
the chain-walking logic is correct.

For an end-to-end check against a real file, compress a test game with
nsz's CLI using `--block --chain N` for a few values of `N`, then point your
reader at the resulting `.nsz`/`.xcz` and confirm it reconstructs the same
bytes as `nsz -D` produces.
