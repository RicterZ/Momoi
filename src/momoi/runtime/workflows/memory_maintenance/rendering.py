from xml.etree.ElementTree import Element

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
            section.append(memory_element(memory))
            supplied.add(memory["id"])
    for memory in memory_directory:
        if memory["id"] not in supplied:
            directory.append(memory_element(memory, compact=True))
            supplied.add(memory["id"])
    return xml_sections(mutable, context, directory, owner_evidence_element(owner_evidence))
