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
        self.assertEqual(len(parents), 16)
        self.assertEqual(len(CATEGORIES), len({x.id for x in CATEGORIES.values()}))
        self.assertEqual({x.id for x in children('fashion')}, {'fashion.clothing','fashion.footwear','fashion.accessories','fashion.jewelry'})
        self.assertTrue(all(x.parent_id in CATEGORIES for x in CATEGORIES.values() if x.parent_id))
        self.assertTrue(all(x.status == 'active' for x in CATEGORIES.values()))
        self.assertEqual(TAXONOMY_VERSION, 'topics-v1.2')
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
        expected = {
            'entertainment':4, 'celebrity_public_figures':11, 'arts_culture':3, 'fashion':4, 'home_living':4,
            'technology':4, 'gaming':3, 'beauty_personal_care':3, 'sports':4,
            'food_beverage':3, 'health_fitness':3, 'travel_lifestyle':3,
            'business_finance':3, 'news_politics':5, 'other':0, 'unclassified':0,
        }
        self.assertEqual({name:len(children(name)) for name in expected}, expected)

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
