"""Bounded storage policy for one normalized YouTube search batch."""

# Shared cache payloads are capped at 4 MB. Leave room for result metadata while
# allowing all 50 provider records to use the durable checkpoint blob path.
MAX_YOUTUBE_BATCH_BYTES = 3_500_000
