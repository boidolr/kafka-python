# This class takes advantage of the fact that all formats v0, v1 and v2 of
# messages storage has the same byte offsets for Length and Magic fields.
# Lets look closely at what leading bytes all versions have:
#
# V0 and V1 (Offset is MessageSet part, other bytes are Message ones):
#  Offset => Int64
#  BytesLength => Int32
#  CRC => Int32
#  Magic => Int8
#  ...
#
# V2:
#  BaseOffset => Int64
#  Length => Int32
#  PartitionLeaderEpoch => Int32
#  Magic => Int8
#  ...
#
# So we can iterate over batches just by knowing offsets of Length. Magic is
# used to construct the correct class for Batch itself.
from __future__ import division

import struct

from kafka.errors import CorruptRecordError, IllegalStateError, UnsupportedVersionError
from kafka.record.abc import ABCRecords
from kafka.record.legacy_records import LegacyRecordBatch, LegacyRecordBatchBuilder
from kafka.record.default_records import DefaultRecordBatch, DefaultRecordBatchBuilder


class MemoryRecords(ABCRecords):

    LENGTH_OFFSET = struct.calcsize(">q")
    LOG_OVERHEAD = struct.calcsize(">qi")
    MAGIC_OFFSET = struct.calcsize(">qii")

    # Minimum space requirements for Record V0
    MIN_SLICE = LOG_OVERHEAD + LegacyRecordBatch.RECORD_OVERHEAD_V0

    __slots__ = ("_buffer", "_pos", "_next_slice", "_remaining_bytes")

    def __init__(self, bytes_data):
        self._buffer = bytes_data
        self._pos = 0
        # We keep one slice ahead so `has_next` will return very fast
        self._next_slice = None
        self._remaining_bytes = None
        self._cache_next()

    def size_in_bytes(self):
        return len(self._buffer)

    def valid_bytes(self):
        # We need to read the whole buffer to get the valid_bytes.
        # NOTE: in Fetcher we do the call after iteration, so should be fast
        if self._remaining_bytes is None:
            next_slice = self._next_slice
            pos = self._pos
            while self._remaining_bytes is None:
                self._cache_next()
            # Reset previous iterator position
            self._next_slice = next_slice
            self._pos = pos
        return len(self._buffer) - self._remaining_bytes

    # NOTE: we cache offsets here as kwargs for a bit more speed, as cPython
    # will use LOAD_FAST opcode in this case
    def _cache_next(self, len_offset=LENGTH_OFFSET, log_overhead=LOG_OVERHEAD):
        buffer = self._buffer
        buffer_len = len(buffer)
        pos = self._pos
        remaining = buffer_len - pos
        if remaining < log_overhead:
            # Will be re-checked in Fetcher for remaining bytes.
            self._remaining_bytes = remaining
            self._next_slice = None
            return

        length, = struct.unpack_from(
            ">i", buffer, pos + len_offset)

        slice_end = pos + log_overhead + length
        if slice_end > buffer_len:
            # Will be re-checked in Fetcher for remaining bytes
            self._remaining_bytes = remaining
            self._next_slice = None
            return

        self._next_slice = memoryview(buffer)[pos: slice_end]
        self._pos = slice_end

    def has_next(self):
        return self._next_slice is not None

    # NOTE: same cache for LOAD_FAST as above
    def next_batch(self, _min_slice=MIN_SLICE,
                   _magic_offset=MAGIC_OFFSET):
        next_slice = self._next_slice
        if next_slice is None:
            return None
        if len(next_slice) < _min_slice:
            raise CorruptRecordError(
                "Record size is less than the minimum record overhead "
                "({})".format(_min_slice - self.LOG_OVERHEAD))
        self._cache_next()
        magic, = struct.unpack_from(">b", next_slice, _magic_offset)
        if magic <= 1:
            return LegacyRecordBatch(next_slice, magic)
        else:
            return DefaultRecordBatch(next_slice)

    def __iter__(self):
        return self

    def __next__(self):
        if not self.has_next():
            raise StopIteration
        return self.next_batch()

    next = __next__


class MemoryRecordsBuilder(object):

    __slots__ = ("_builder", "_batch_size", "_buffer", "_next_offset", "_closed",
                 "_magic", "_bytes_written", "_producer_id", "_producer_epoch")

    def __init__(self, magic, compression_type, batch_size, offset=0,
                 transactional=False, producer_id=-1, producer_epoch=-1, base_sequence=-1):
        assert magic in [0, 1, 2], "Not supported magic"
        assert compression_type in [0, 1, 2, 3, 4], "Not valid compression type"
        if magic >= 2:
            assert not transactional or producer_id != -1, "Cannot write transactional messages without a valid producer ID"
            assert producer_id == -1 or producer_epoch != -1, "Invalid negative producer epoch"
            assert producer_id == -1 or base_sequence != -1, "Invalid negative sequence number used"

            self._builder = DefaultRecordBatchBuilder(
                magic=magic, compression_type=compression_type,
                is_transactional=transactional, producer_id=producer_id,
                producer_epoch=producer_epoch, base_sequence=base_sequence,
                batch_size=batch_size)
            self._producer_id = producer_id
            self._producer_epoch = producer_epoch
        else:
            assert not transactional and producer_id == -1, "Idempotent messages are not supported for magic %s" % (magic,)
            self._builder = LegacyRecordBatchBuilder(
                magic=magic, compression_type=compression_type,
                batch_size=batch_size)
            self._producer_id = None
        self._batch_size = batch_size
        self._buffer = None

        self._next_offset = offset
        self._closed = False
        self._magic = magic
        self._bytes_written = 0

    def skip(self, offsets_to_skip):
        # Exposed for testing compacted records
        self._next_offset += offsets_to_skip

    def append(self, timestamp, key, value, headers=[]):
        """ Append a message to the buffer.

        Returns: RecordMetadata or None if unable to append
        """
        if self._closed:
            return None

        offset = self._next_offset
        metadata = self._builder.append(offset, timestamp, key, value, headers)
        # Return of None means there's no space to add a new message
        if metadata is None:
            return None

        self._next_offset += 1
        return metadata

    def set_producer_state(self, producer_id, producer_epoch, base_sequence, is_transactional):
        if self._magic < 2:
            raise UnsupportedVersionError('Producer State requires Message format v2+')
        elif self._closed:
            # Sequence numbers are assigned when the batch is closed while the accumulator is being drained.
            # If the resulting ProduceRequest to the partition leader failed for a retriable error, the batch will
            # be re queued. In this case, we should not attempt to set the state again, since changing the pid and sequence
            # once a batch has been sent to the broker risks introducing duplicates.
            raise IllegalStateError("Trying to set producer state of an already closed batch. This indicates a bug on the client.")
        self._builder.set_producer_state(producer_id, producer_epoch, base_sequence, is_transactional)
        self._producer_id = producer_id

    @property
    def producer_id(self):
        return self._producer_id

    @property
    def producer_epoch(self):
        return self._producer_epoch

    def records(self):
        assert self._closed
        return MemoryRecords(self._buffer)

    def close(self):
        # This method may be called multiple times on the same batch
        # i.e., on retries
        # we need to make sure we only close it out once
        # otherwise compressed messages may be double-compressed
        # see Issue 718
        if not self._closed:
            self._bytes_written = self._builder.size()
            self._buffer = bytes(self._builder.build())
            if self._magic == 2:
                self._producer_id = self._builder.producer_id
                self._producer_epoch = self._builder.producer_epoch
            self._builder = None
        self._closed = True

    def size_in_bytes(self):
        if not self._closed:
            return self._builder.size()
        else:
            return len(self._buffer)

    def compression_rate(self):
        assert self._closed
        return self.size_in_bytes() / self._bytes_written

    def is_full(self):
        if self._closed:
            return True
        else:
            return self._builder.size() >= self._batch_size

    def next_offset(self):
        return self._next_offset

    def buffer(self):
        assert self._closed
        return self._buffer
