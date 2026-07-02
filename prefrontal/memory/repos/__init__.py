"""Per-domain repository mixins composed by MemoryStore.

Each class here owns the reads/writes for one slice of the schema. They assume
the connection/scoping core (``self.conn`` / ``self._uid()``) provided by
:class:`prefrontal.memory.store.MemoryStore`, which mixes them together.
"""
