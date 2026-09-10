from xml.etree.ElementTree import Element

from ....storage import memory_snapshot_fingerprint
from ..memory_rendering import memory_element, owner_evidence_element, xml_sections


def render_memory_maintenance_request(*, mutable_memories, context_memories,
                                      memory_directory, owner_evidence):
    mutable = Element("mutable_memories")
    context = Element("context_memories")
    directory = Element("memory_directory")
    supplied = set()
    for rows, section in ((mutable_memories, mutable), (context_memories, context)):
        for memory in rows:
            if memory["id"] in supplied:
                continue
            node = memory_element(memory)
            node.set("snapshot_fingerprint", memory_snapshot_fingerprint(memory))
            section.append(node)
            supplied.add(memory["id"])
    for memory in memory_directory:
        if memory["id"] not in supplied:
            directory.append(memory_element(memory, compact=True))
            supplied.add(memory["id"])
    return xml_sections(mutable, context, directory, owner_evidence_element(owner_evidence))
