import tempfile
import unittest
from pathlib import Path

import httpx

from app.collections.entity_context import WikimediaEntityContextResolver
from app.collections.store import CollectionStore


def claim(identifier):
    return [{"mainsnak": {"datavalue": {"value": {"id": identifier}}}}]


class EntityContextTests(unittest.TestCase):
    def test_siteinfo_identifies_the_main_page_and_localized_namespace_without_title_guessing(self):
        def handler(request):
            if request.url.host == "www.mediawiki.org":
                return httpx.Response(200, json={"query": {
                    "general": {"mainpage": "MediaWiki"},
                    "pages": [{"pageid": 1, "ns": 0, "title": "MediaWiki", "pageprops": {}}],
                }}, request=request)
            if request.url.host == "id.wiktionary.org":
                return httpx.Response(200, json={"query": {
                    "general": {"mainpage": "Halaman Utama"},
                    "namespacealiases": [{"id": -1, "alias": "Istimewa"}],
                    "pages": [],
                }}, request=request)
            return httpx.Response(500, request=request)

        with tempfile.TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite")
            records = []
            with store.connect() as db:
                for identifier, title, project in (
                    ("homepage", "MediaWiki", "mediawiki"),
                    ("special", "Istimewa:Pencarian", "id.wiktionary"),
                ):
                    db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                        data_mode,evidence,context) VALUES(?,?,?, ?,?,'2026-11-13','LIVE','{}','')""",
                        (identifier, title, title.casefold(), "wikimedia_pageviews_top_per_country", project))
                    records.append({"id":identifier,"keyword":title,"project":project,
                                   "source":"wikimedia_pageviews_top_per_country"})
            resolver = WikimediaEntityContextResolver(store, transport=httpx.MockTransport(handler))
            contexts = resolver.enrich(records)
            self.assertTrue(contexts["homepage"].is_main_page)
            self.assertEqual(contexts["homepage"].page_namespace, 0)
            self.assertEqual(contexts["special"].page_namespace, -1)
            self.assertFalse(contexts["special"].is_main_page)
            self.assertEqual(resolver.last_stats["requested_titles"], 2)

    def test_project_host_is_allowlisted_and_special_namespace_is_retained(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "id.wiktionary.org":
                return httpx.Response(200, json={"query": {"pages": [{"pageid": 3, "ns": -1,
                    "title": "Istimewa:Pencarian", "pagelanguage": "id", "fullurl":
                    "https://id.wiktionary.org/wiki/Istimewa:Pencarian", "pageprops": {}}]}}, request=request)
            return httpx.Response(500, request=request)

        with tempfile.TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite")
            with store.connect() as db:
                db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                    data_mode,evidence,context) VALUES('special','Istimewa:Pencarian','istimewa:pencarian',
                    'wikimedia_pageviews_top_per_country','id.wiktionary','2026-11-13','LIVE','{}','')""")
            records = [{"id":"special", "keyword":"Istimewa:Pencarian", "project":"id.wiktionary",
                        "source":"wikimedia_pageviews_top_per_country"}]
            result = WikimediaEntityContextResolver(store, transport=httpx.MockTransport(handler)).enrich(records)
            special = result["special"]
            self.assertEqual((special.page_namespace, special.page_language), (-1, "id"))
            self.assertFalse(special.is_disambiguation)
            self.assertEqual(calls, ["id.wiktionary.org"])

            blocked = [{"id":"blocked", "keyword":"Some title", "project":"attacker.example.wikipedia",
                        "source":"wikimedia_pageviews_top_per_country"}]
            self.assertEqual(WikimediaEntityContextResolver(store,
                transport=httpx.MockTransport(handler)).enrich(blocked), {})
            self.assertEqual(calls, ["id.wiktionary.org"], "unlisted project strings must not create hosts")

    def test_wikipedia_page_binding_and_wikidata_roles_are_cached(self):
        titles = [
            ("amanda", "Amanda Seyfried", "Q100", "Q33999"),
            ("aoc", "Alexandria Ocasio-Cortez", "Q101", "Q82955"),
            ("ansel", "Ansel Adams", "Q102", "Q33231"),
            ("alina", "Alina Habba", "Q103", "Q40348"),
        ]
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.host == "en.wikipedia.org":
                pages = [{"pageid": index + 10, "title": title,
                          "description": "A verified encyclopedia description.",
                          "pageprops": {"wikibase_item": qid}}
                         for index, (_key, title, qid, _occupation) in enumerate(titles)]
                return httpx.Response(200, json={"query": {"pages": pages}}, request=request)
            entities = {}
            for _key, _title, qid, occupation in titles:
                entities[qid] = {"descriptions": {"en": {"value": "Verified person description"}},
                                 "claims": {"P31": claim("Q5"), "P106": claim(occupation)}}
            return httpx.Response(200, json={"entities": entities}, request=request)

        with tempfile.TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite")
            records = []
            with store.connect() as db:
                for key, title, _qid, _occupation in titles:
                    db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                        data_mode,evidence,context) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (key, title, title.casefold(), "wikimedia_pageviews_top_per_country",
                         "en.wikipedia", "2026-11-11", "LIVE", "{}", ""))
                    records.append({"id": key, "keyword": title, "project": "en.wikipedia",
                                    "source": "wikimedia_pageviews_top_per_country"})
            resolver = WikimediaEntityContextResolver(store, transport=httpx.MockTransport(handler))
            first = resolver.enrich(records)
            self.assertEqual(len(calls), 2)
            self.assertEqual(first["amanda"].entity_type, "person")
            self.assertIn("celebrity_public_figures.performers", first["amanda"].category_ids)
            self.assertIn("celebrity_public_figures.politicians", first["aoc"].category_ids)
            self.assertNotIn("news_politics.public_figures", first["aoc"].category_ids)
            self.assertIn("arts_culture.photography", first["ansel"].category_ids)
            self.assertIn("celebrity_public_figures.legal_professionals", first["alina"].category_ids)
            self.assertTrue(all(item.entity_id and item.source_url and item.description
                                for item in first.values()))
            resolver.enrich(records)
            self.assertEqual(len(calls), 2, "cached context must not trigger repeat API calls")

    def test_political_event_uses_title_and_description_to_distinguish_elections(self):
        items = [("election", "2028 United States Senate elections", "Q201", "National election"),
                 ("protest", "2026 political protest", "Q202", "Political demonstration")]

        def handler(request):
            if request.url.host == "en.wikipedia.org":
                return httpx.Response(200, json={"query": {"pages": [
                    {"pageid": index, "title": title, "description": description,
                     "pageprops": {"wikibase_item": qid}}
                    for index, (_key, title, qid, description) in enumerate(items, start=1)]}}, request=request)
            return httpx.Response(200, json={"entities": {
                qid: {"descriptions": {"en": {"value": description}},
                      "claims": {"P31": claim("Q40231")}}
                for _key, _title, qid, description in items}}, request=request)

        with tempfile.TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite")
            records = []
            with store.connect() as db:
                for key, title, _qid, _description in items:
                    db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                        data_mode,evidence,context) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (key,title,title.casefold(),"wikimedia_pageviews_top_per_country","en.wikipedia",
                         "2026-11-11","LIVE","{}",""))
                    records.append({"id":key,"keyword":title,"project":"en.wikipedia",
                                    "source":"wikimedia_pageviews_top_per_country"})
            result = WikimediaEntityContextResolver(store,
                transport=httpx.MockTransport(handler)).enrich(records)
            self.assertIn("news_politics.elections", result["election"].category_ids)
            self.assertIn("news_politics.current_events", result["protest"].category_ids)

    def test_api_failure_fails_closed_without_losing_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CollectionStore(Path(directory) / "collections.sqlite")
            with store.connect() as db:
                db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                    data_mode,evidence,context) VALUES('x','Ambiguous Name','ambiguous name',?,?,'2026-11-11','LIVE','{}','')""",
                    ("wikimedia_pageviews_top_per_country", "en.wikipedia"))
            resolver = WikimediaEntityContextResolver(store,
                transport=httpx.MockTransport(lambda request: httpx.Response(429, request=request)))
            result = resolver.enrich([{"id": "x", "keyword": "Ambiguous Name",
                "project": "en.wikipedia", "source": "wikimedia_pageviews_top_per_country"}])
            self.assertEqual(result, {})
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM keywords").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM entity_context").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
