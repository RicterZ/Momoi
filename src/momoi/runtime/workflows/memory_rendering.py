"""Model-facing memory records, separate from complete database snapshots."""

from xml.etree.ElementTree import Element, SubElement, tostring


def memory_record(memory):
    fields = ("id", "kind", "key", "activation", "content", "updated_at", "expires_at",
              "source_event_id", "evidence_quote")
    return {key: memory[key] for key in fields if memory.get(key) is not None}


def memory_element(memory, *, visible=False, compact=False):
    record = memory_record(memory)
    attrs = {key: str(record[key]) for key in ("id", "kind", "key", "activation")}
    if visible:
        attrs["visible"] = "true"
    if not compact:
        attrs.update({key: str(record[key]) for key in ("updated_at", "expires_at") if key in record})
    node = Element("memory", attrs)
    content = str(record["content"])
    if compact and len(content) > 240:
        content = content[:237] + "..."
    SubElement(node, "content").text = content
    if not compact and record.get("evidence_quote"):
        SubElement(node, "evidence", {"event_id": str(record["source_event_id"])}).text = str(record["evidence_quote"])
    return node


def owner_evidence_element(records):
    node = Element("owner_evidence")
    for record in records:
        attrs = {"id": str(record["event_id"])}
        for field, attr in (("occurred_at", "at"), ("occurred_at_unix", "unix")):
            if record.get(field) is not None:
                attrs[attr] = str(record[field])
        SubElement(node, "event", attrs).text = str(record["content"])
    return node


def xml_sections(*nodes):
    return "\n\n".join(tostring(node, encoding="unicode") for node in nodes)
