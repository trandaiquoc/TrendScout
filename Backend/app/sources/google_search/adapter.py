"""Google SERP adapter through the shared offline SerpApi boundary."""

from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.serpapi.adapter import SerpApiSearchAdapter


class GoogleSearchAdapter(SerpApiSearchAdapter):
    def __init__(self, client, **kwargs):
        super().__init__(GOOGLE_SOURCE_ID, client, **kwargs)
