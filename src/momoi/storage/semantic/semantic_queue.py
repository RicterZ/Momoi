from .semantic_documents import semantic_source_key


class SemanticQueueStore:
    def recover_semantic_encoding(self) -> int:
        return self.memory_index_queue.recover_encoding()

    def claim_semantic_documents(self, space_id: str, limit: int):
        return self.memory_index_queue.claim_documents(space_id, limit)

    def finish_semantic_documents(self, rows, vectors, dimensions):
        return self.memory_index_queue.finish_documents(
            rows, vectors, dimensions, [semantic_source_key(row) for row in rows],
        )

    def fail_semantic_documents(self, rows, error):
        self.memory_index_queue.fail_documents(rows, error)

    def semantic_ready_documents(self, space_id: str, *, page_size: int = 512):
        return self.memory_vectors.ready_documents(space_id, page_size=page_size)

    def semantic_ready_source_documents(self, space_id: str, source_type: str, source_id: str):
        return self.memory_vectors.ready_source_documents(
            space_id, "episode_summary" if source_type == "episode" else source_type,
            source_id, include_children=source_type == "episode",
        )

    def invalidate_semantic_document(self, space_id, document_type, source_id, chunk_index, error):
        self.memory_vectors.invalidate(space_id, document_type, source_id, chunk_index, error)
