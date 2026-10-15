"""An open-ended exploration route that runs the pi agent harness, only for tasks that need no safety machinery.

The deterministic graph (access plan -> plan -> gate -> run -> validate -> verify) stays the path for anything with a login,
a credential, a sensitive action, a card or an approval. `router.explore_eligible` decides that in code, from the same access
plan the graph already builds. Pi itself is boxed in: no built-in tools (no shell, no files), one extension with two tools
that call back into Nami's guarded web access, a minimal environment, an empty working folder, and no vault.
"""
