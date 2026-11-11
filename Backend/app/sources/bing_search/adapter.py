"""Bing SERP adapter through the shared offline SerpApi boundary."""

from app.sources.catalog import BING_SOURCE_ID
from app.sources.serpapi.adapter import SerpApiSearchAdapter


class BingSearchAdapter(SerpApiSearchAdapter):
    def __init__(self, client, **kwargs):
        super().__init__(BING_SOURCE_ID, client, **kwargs)
