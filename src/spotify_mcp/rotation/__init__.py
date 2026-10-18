"""Daily playlist rotation: a persistent song pool, the six rotation rules, and the tools the
scheduled morning run calls. Layering: tools/rotation.py (thin) -> service.py (the operations)
-> store.py (sqlite) + engine.py (pure selection) + the SpotifyClient for every Spotify call."""
