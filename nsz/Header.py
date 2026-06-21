class Section:
    def __init__(self, f):
        self.f = f
        self.offset = f.readInt64()
        self.size = f.readInt64()
        self.cryptoType = f.readInt64()
        f.readInt64()  # padding
        self.cryptoKey = f.read(16)
        self.cryptoCounter = f.read(16)


class FakeSection:
    def __init__(self, offset, size):
        self.offset = offset
        self.size = size
        self.cryptoType = 1


class Block:
    # type 1: independent blocks, each its own zstd frame, no shared dictionary
    # type 2: chained blocks, every Nth block (chainLength) is independent and
    #         the blocks in between are compressed using the previous block's
    #         raw bytes as a zstd content dictionary - see docs/SeekableCompression.md
    SUPPORTED_TYPES = (1, 2)

    def __init__(self, f):
        self.f = f
        self.magic = f.read(8)
        self.version = f.readInt8()
        self.type = f.readInt8()
        # 3rd header byte: unused for type 1, chainLength for type 2
        self.chainLength = f.readInt8()
        self.blockSizeExponent = f.readInt8()
        self.numberOfBlocks = f.readInt32()
        self.decompressedSize = f.readInt64()
        self.compressedBlockSizeList = [
            f.readInt32() for _ in range(self.numberOfBlocks)
        ]
        if self.type not in self.SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported NCZBLOCK type {self.type}. Please update nsz to a "
                "version that supports this file."
            )
        if self.type == 2 and self.chainLength < 1:
            raise ValueError(
                "Corrupted NCZBLOCK header: chainLength must be at least 1"
            )
