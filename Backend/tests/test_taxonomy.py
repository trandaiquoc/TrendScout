"""Synthetic taxonomy and classification contracts; no external services."""
import ast
import unittest
from pathlib import Path

from test_keywords import observation

from app.analysis.keywords import extract_keyword, filter_keywords
from app.taxonomy.classifier import CLASSIFICATION_VERSION, RULES, classify
from app.taxonomy.registry import (
    ALIASES,
    CATEGORIES,
    TAXONOMY_VERSION,
    children,
    map_legacy_categories,
    resolve,
)

SOURCE = 'wikimedia_pageviews_top_per_country'


class TaxonomyRegistryTests(unittest.TestCase):
    def test_parents_subcategories_and_unique_ids(self):
        parents = [x for x in CATEGORIES.values() if x.parent_id is None]
        self.assertGreaterEqual(len(parents), 20)
        self.assertEqual(len(CATEGORIES), len({x.id for x in CATEGORIES.values()}))
        self.assertEqual({x.id for x in children('fashion')}, {'fashion.clothing','fashion.footwear','fashion.accessories','fashion.jewelry'})
        self.assertTrue(all(x.parent_id in CATEGORIES for x in CATEGORIES.values() if x.parent_id))
        self.assertTrue(all(x.status == 'active' for x in CATEGORIES.values()))
        self.assertEqual(TAXONOMY_VERSION, 'topics-v1.4')
        for rule in RULES:
            self.assertIsNone(resolve(rule.category_id).parent_id)
            if rule.subcategory_id:
                self.assertEqual(resolve(rule.subcategory_id).parent_id, rule.category_id)

    def test_aliases_unknown_and_immutable_registry(self):
        self.assertEqual(ALIASES['home_goods'], 'home_living')
        self.assertEqual(resolve('home_goods').id, 'home_living')
        self.assertEqual(map_legacy_categories(('fashion','home_goods')), ('fashion','home_living'))
        self.assertEqual(map_legacy_categories(('unclassified',)), ('unclassified',))
        with self.assertRaises(ValueError):
            map_legacy_categories(('technology',))
        self.assertEqual(resolve('entertainment.film').parent_id, 'entertainment')
        with self.assertRaises(ValueError):
            resolve('Fashion')
        with self.assertRaises(ValueError):
            children('fashion.clothing')
        with self.assertRaises(TypeError):
            CATEGORIES['arbitrary'] = resolve('fashion')

    def test_all_required_topics_have_expected_children(self):
        expected_ids = {'events.historical', 'events.festivals', 'science_nature.chemistry',
            'places_transportation.ships', 'organizations.government', 'products_brands.software',
            'celebrity_public_figures.coaches', 'celebrity_public_figures.entrepreneurs'}
        self.assertTrue(expected_ids.issubset(CATEGORIES))

    def test_taxonomy_does_not_import_sdk_or_web(self):
        directory = Path(__file__).resolve().parents[1] / 'app' / 'taxonomy'
        for path in directory.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                else:
                    continue
                self.assertFalse(any(n.startswith(('firebase_admin','google.cloud','fastapi','httpx')) for n in names), path.name)


class ClassificationTests(unittest.TestCase):
    def classify(self, title, *, project='en.wikipedia', source=SOURCE):
        return classify('synthetic-keyword-id', title, source=source, project=project)

    def test_multiple_categories_evidence_and_independent_attributes(self):
        result = self.classify('Café fashion week and washing machine')
        self.assertEqual(result.topic_categories, ('fashion','home_living'))
        self.assertEqual(result.subcategories, ('home_living.appliances',))
        self.assertEqual((result.entity_type, result.commercial_relevance), ('unknown','not_assessed'))
        self.assertEqual((result.classification_version, result.taxonomy_version), (CLASSIFICATION_VERSION,TAXONOMY_VERSION))
        self.assertEqual(result.review_status,'unreviewed')
        self.assertEqual([(e.rule_id,e.source,e.project,e.context) for e in result.rule_evidence],
                         [('fashion_week',SOURCE,'en.wikipedia','article_title'),('washing_machine',SOURCE,'en.wikipedia','article_title')])
        self.assertTrue(all(e.rule_version == '1' and e.explanation for e in result.rule_evidence))

    def test_conflict_policy_film_context(self):
        result = self.classify('The Washing Machine (film)')
        self.assertEqual(result.topic_categories, ('entertainment',))
        self.assertEqual(result.subcategories, ('entertainment.film',))
        self.assertEqual(result.rule_evidence[0].rule_id, 'film_qualifier')
        self.assertEqual(self.classify('Fashion Week (song)').topic_categories, ('entertainment',))
        self.assertEqual(self.classify('Fashion Week (song)').subcategories, ('entertainment.music',))

    def test_context_ambiguous_and_unicode_do_not_invent_evidence(self):
        for title in ('Stephen Miller','Apple','Fashion','Home','Fashion weekends','洗濯機'):
            with self.subTest(title=title):
                result = self.classify(title)
                self.assertEqual(result.topic_categories, ('unclassified',))
                self.assertEqual(result.rule_evidence, ())
                self.assertEqual(result.classification_status, 'unclassified')
        self.assertEqual(self.classify('Fashion Week', project='fr.wikipedia').topic_categories, ('unclassified',))
        self.assertEqual(self.classify('Fashion Week', source='other').topic_categories, ('unclassified',))
        self.assertEqual(self.classify('Cafe\u0301 fashion week').rule_evidence[0].matched_text, 'fashion week')
        self.assertNotIn('other', self.classify('Stephen Miller').topic_categories)

    def test_election_and_input_validation(self):
        result = self.classify('2028 United States Senate elections')
        self.assertEqual(result.topic_categories, ('news_politics',))
        self.assertEqual(result.subcategories, ('news_politics.elections',))
        with self.assertRaises(ValueError):
            self.classify('')
        with self.assertRaises(ValueError):
            classify('', 'film', source=SOURCE, project='en.wikipedia')

    def test_work_types_require_explicit_media_context_and_persons_stay_unknown(self):
        expected = {
            'Godzilla (2024 film)': ('entertainment', 'entertainment.film'),
            'Dancing with the Stars (television program)': ('entertainment', 'entertainment.television'),
            'Game Title (video game)': ('gaming', 'gaming.video_games'),
        }
        for title, (category, subcategory) in expected.items():
            with self.subTest(title=title):
                result = self.classify(title)
                self.assertEqual(result.topic_categories, (category,))
                self.assertEqual(result.subcategories, (subcategory,))
                self.assertEqual(result.entity_type, 'unknown')
        for title in ('Amanda Seyfried', 'Alexandria Ocasio-Cortez', 'Ansel Adams', 'Alina Habba'):
            with self.subTest(title=title):
                result = self.classify(title)
                self.assertEqual(result.entity_type, 'unknown')
                self.assertEqual(result.topic_categories, ('unclassified',))

    def test_multilingual_events_and_science_rules_have_grounded_evidence(self):
        election = self.classify('Elecciones generales', project='es.wikipedia')
        self.assertEqual(election.topic_categories, ('unclassified',))
        vietnamese = self.classify('Bầu cử tổng thống', project='vi.wikipedia')
        self.assertEqual(vietnamese.topic_categories, ('news_politics',))
        self.assertEqual(vietnamese.subcategories, ('news_politics.elections',))
        historical = self.classify('World War II')
        self.assertEqual(historical.subcategories, ('events.historical',))
        self.assertEqual(historical.rule_evidence[0].rule_id, 'historical_event')

    def test_contextual_classification_separates_unknown_and_verified_multilabel(self):
        from app.taxonomy.classifier import classify_contextual
        unknown = classify_contextual({"id":"x", "keyword":"Taylor Smith", "source":SOURCE,
            "project":"en.wikipedia", "eligible":1}, {})
        self.assertEqual(unknown["classification_status"], "UNKNOWN")
        person = classify_contextual({"id":"y", "keyword":"Jensen Huang", "source":SOURCE,
            "project":"en.wikipedia", "eligible":1}, {"entity_type":"person", "entity_id":"Q123",
            "description":"Business executive", "occupations":("Q43845",), "category_ids":(),
            "instance_types":("Q5",)})
        self.assertEqual(person["classification_status"], "CLASSIFIED")
        self.assertEqual(person["topic_category_ids"], ["celebrity_public_figures.business_leaders"])
        self.assertEqual(person["confidence_level"], "HIGH")
        company = classify_contextual({"id":"company", "keyword":"Unfamiliar name", "source":SOURCE,
            "project":"en.wikipedia", "eligible":1}, {"entity_type":"unknown", "entity_id":"Q901",
            "description":"American multinational technology company", "occupations":(), "category_ids":(),
            "instance_types":(), "source_url":"https://en.wikipedia.org/wiki/Example"})
        self.assertEqual(company["classification_status"], "CLASSIFIED")
        self.assertIn("organizations", company["topic_category_ids"])
        self.assertIn("technology", company["topic_category_ids"])
        self.assertTrue(any(item.get("kind") == "ENTITY_DESCRIPTION_RULE" for item in company["rule_evidence"]))
        ineligible = classify_contextual({"id":"z", "keyword":"technical noise", "source":SOURCE,
            "project":"en.wikipedia", "eligible":False}, {})
        self.assertEqual(ineligible["classification_status"], "INELIGIBLE")

    def test_wikidata_instance_type_map_covers_non_person_entities(self):
        from app.collections.entity_context import INSTANCE_TYPES
        from app.taxonomy.classifier import classify_contextual
        examples = {
            "Q43229": "organizations", "Q431289": "products_brands",
            "Q11424": "entertainment.film", "Q7889": "gaming.video_games",
            "Q40231": "news_politics.elections", "Q16510064": "sports.competitions",
            "Q6256": "places_transportation.countries", "Q515": "places_transportation.cities",
            "Q34442": "places_transportation.roads", "Q11446": "places_transportation.ships",
            "Q11436": "places_transportation.aircraft", "Q11173": "science_nature.chemistry",
            "Q7432": "science_nature.biology", "Q729": "science_nature.animals",
            "Q756": "science_nature.plants", "Q17537576": "science_nature.concepts",
            "Q571": "creative_works.books",
        }
        for index, (instance_id, category_id) in enumerate(examples.items()):
            with self.subTest(instance_id=instance_id):
                title = "2026 United States elections" if instance_id == "Q40231" else "Evidence-backed entity"
                result = classify_contextual({"id":str(index), "keyword":title,
                    "source":SOURCE, "project":"en.wikipedia", "eligible":1},
                    {"entity_type":"unknown", "entity_id":"Q900", "instance_types":(instance_id,),
                     "occupations":(), "category_ids":(), "description":"Wikidata instance evidence"})
                self.assertIn(category_id, result["topic_category_ids"])
                self.assertEqual(result["classification_status"], "CLASSIFIED")
                self.assertTrue(any(item.get("kind") == "WIKIDATA_ENTITY" for item in result["rule_evidence"]))
                resolve(category_id)
        self.assertIn("Q5", INSTANCE_TYPES)

    def test_owner_supplied_regression_title_fixture_never_guesses_bare_names(self):
        from app.taxonomy.classifier import classify_contextual
        fixture = Path(__file__).parent / "fixtures" / "contextual_classification_titles.txt"
        titles = fixture.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(titles), 60)
        self.assertEqual(len(titles), len(set(titles)))
        bare_names = {"Jensen Huang", "Lisa Su", "Carson Wentz", "Nvidia", "United States", "MediaWiki"}
        for index, title in enumerate(titles):
            result = classify_contextual({"id":str(index), "keyword":title, "source":SOURCE,
                "project":"en.wikipedia", "eligible":1}, {})
            if title in bare_names:
                self.assertEqual(result["classification_status"], "UNKNOWN", title)
                self.assertNotIn("celebrity_public_figures.public_figures", result["topic_category_ids"])
        election = classify_contextual({"id":"fixture-election", "keyword":"2026 United States elections",
            "source":SOURCE, "project":"en.wikipedia", "eligible":1}, {})
        self.assertEqual(election["topic_category_ids"], ["news_politics.elections"])
        film = classify_contextual({"id":"fixture-film", "keyword":"Wildwood (film)",
            "source":SOURCE, "project":"en.wikipedia", "eligible":1}, {})
        self.assertEqual(film["topic_category_ids"], ["entertainment.film"])

    def test_owner_25_contextual_cases_use_saved_evidence_and_stay_conservative_without_it(self):
        import json
        from app.taxonomy.classifier import classify_contextual

        fixture = Path(__file__).parent / "fixtures" / "contextual_classification_cases.json"
        cases = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 25)
        self.assertEqual(len({item["title"] for item in cases}), 25)
        for index, item in enumerate(cases):
            bare = classify_contextual({"id":f"bare-{index}", "keyword":item["title"],
                "source":SOURCE, "project":item["project"], "eligible":1}, {})
            self.assertEqual(bare["classification_status"], "UNKNOWN", item["title"])
            self.assertEqual(bare["topic_category_ids"], [], item["title"])
            if not item.get("description") and "expected_status" not in item:
                continue
            context = {"entity_type":item["entity_type"], "entity_id":item["entity_id"],
                "description":item["description"], "occupations":tuple(item["occupations"]),
                "instance_types":tuple(item["instance_types"]), "category_ids":(),
                "source_url":item.get("source_url", ""), "method":item.get("method", ""),
                "page_namespace":item.get("page_namespace")}
            result = classify_contextual({"id":f"context-{index}", "keyword":item["title"],
                "source":SOURCE, "project":item["project"], "eligible":1}, context)
            self.assertEqual(result["classification_status"], item.get("expected_status", "CLASSIFIED"), item["title"])
            self.assertTrue(set(item["expected_categories"]).issubset(result["topic_category_ids"]),
                            (item["title"], result["topic_category_ids"]))
            if result["classification_status"] == "CLASSIFIED":
                self.assertTrue(result["rule_evidence"], item["title"])
        special = next(item for item in cases if item["title"] == "Istimewa:Pencarian")
        result = classify_contextual({"id":"special", "keyword":special["title"],
            "source":SOURCE, "project":special["project"], "eligible":1},
            {"page_namespace":-1, "entity_type":"unknown"})
        self.assertEqual(result["classification_status"], "INELIGIBLE")

    def test_legacy_behavior_and_serialization_unchanged(self):
        titles = ('Paris Fashion Week', 'washing machine', 'General election', 'Stephen Miller')
        records = [extract_keyword(observation(title)) for title in titles]
        self.assertEqual([x.categories for x in records], [('fashion',),('home_goods',),('other',),('unclassified',)])
        self.assertEqual(len(filter_keywords(records, 'all').items), 4)
        self.assertEqual(len(filter_keywords(records, 'home_goods').items), 1)
        self.assertEqual(len(filter_keywords(records, 'unclassified').items), 1)
        self.assertEqual(records[0].classification_version, 'title-rules-v0.1')
        self.assertNotIn('topic_categories', records[0].to_record())
        self.assertNotIn('taxonomy_version', records[0].to_record())
        old = records[0].to_record()
        self.assertEqual(old['classification_evidence'][0], {'category':'fashion','rule_id':'fashion_industry','matched_text':'Fashion Week'})
