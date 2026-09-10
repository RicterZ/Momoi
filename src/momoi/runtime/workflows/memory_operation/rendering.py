from xml.etree.ElementTree import Element, SubElement

from ..memory_rendering import memory_element, owner_evidence_element, xml_sections


def render_memory_operation_request(*, now, timestamp, max_recent_ttl_hours,
                                    operations, visible, snapshots, evidence):
    clock = Element("current_time", {"at": timestamp, "unix": str(now),
                                     "max_recent_ttl_hours": str(max_recent_ttl_hours)})
    requests = Element("operation_requests")
    for operation in operations:
        attrs = {key: str(operation[key]) for key in ("id", "type", "target_id") if key in operation}
        request = SubElement(requests, "operation", attrs)
        SubElement(request, "content").text = operation["content"]
        SubElement(request, "evidence", {"event_id": operation["event_id"]}).text = operation["evidence"]
    memories = Element("current_memories")
    for memory_id, memory in snapshots.items():
        memories.append(memory_element(memory, visible=memory_id in visible))
    outdated = Element("outdated_visible_snapshots")
    for memory_id, memory in visible.items():
        if snapshots.get(memory_id) != memory:
            outdated.append(memory_element(memory))
    return xml_sections(clock, requests, memories, outdated, owner_evidence_element(evidence))
