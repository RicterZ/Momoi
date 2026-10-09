"""Temporary host adapter; command validation belongs to the memory library."""
from ....memory.writing.validation import parse_decisions as validate_decisions
from ....storage.memory.memory_values import MOMOI_MEMORY_TAGS


def parse_decisions(arguments, operations, memories, evidence):
    return validate_decisions(arguments, operations, memories, evidence, tags=MOMOI_MEMORY_TAGS)
