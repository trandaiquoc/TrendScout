"""Synthetic offline sidecar tests; scores are not trend scores."""
from dataclasses import replace
from datetime import date
import unittest

from app.analysis.keywords import extract_keyword
from app.models.observations import FetchStatus
from app.processing.language_detection import detect_language
from app.processing.quality_analysis import AnalysisConfig, analyze
from app.processing.similarity import candidates
from app.processing.wikimedia_cleaning import clean_snapshot
from test_keywords import observation
from test_wikimedia_cleaning import row, snapshot


def keyword(title, **changes):
    return extract_keyword(replace(observation(title), **changes))


def report_for(n):
    return clean_snapshot(snapshot(n), [row('Synthetic_' + str(i)) for i in range(n)]).report


class LanguageTests(unittest.TestCase):
    def test_english_vietnamese_uncertainty_and_mixed(self):
        self.assertEqual(detect_language('the history of the washing machine').language, 'en')
        self.assertEqual(detect_language('những câu chuyện của người dân').language, 'vi')
        for text in ('Stephen Miller', 'việt nam', '東京', '123 !', '', 'the story của người dân'):
            with self.subTest(text=text):
                result = detect_language(text)
                self.assertEqual(result.language, 'unknown')
                self.assertIsNone(result.confidence)
                self.assertEqual(result.original_text, text)
        self.assertEqual(detect_language('the story của người dân').reason, 'mixed_or_ambiguous_language')
        self.assertEqual(detect_language('the history of the washing machine'), detect_language('the history of the washing machine'))
        with self.assertRaises(ValueError): detect_language(None)


class SimilarityTests(unittest.TestCase):
    def test_candidates_cross_project_and_no_merge(self):
        a = keyword('The Blue Moon Festival')
        b = keyword('The Blue Moon Festival', project='fr.wikipedia')
        sequel = keyword('The Blue Moon Festival 2')
        other_day = replace(a, observation_id='different-day', observation_date=date(2026,11,5))
        other_source = keyword('The Blue Moon Festival', source='other_source')
        rows = [a,b,sequel,other_day,other_source]
        result, status, reason, comparisons = candidates(rows, threshold=.8)
        self.assertEqual((status, reason), ('COMPLETED','candidate_only'))
        self.assertTrue(any({c.left.project,c.right.project} == {'en.wikipedia','fr.wikipedia'} and c.reason == 'cross_project_review_required' for c in result))
        self.assertTrue(any(c.left.article_original.endswith('_2') or c.right.article_original.endswith('_2') for c in result))
        self.assertTrue(all(c.left.keyword_id != c.right.keyword_id for c in result))
        self.assertTrue(all(c.left.source == c.right.source for c in result))
        self.assertTrue(all(c.review_status == 'unreviewed' for c in result))
        self.assertEqual(a.views_ceil, 10)
        self.assertGreater(comparisons, 0)

    def test_threshold_limits_and_deterministic_order(self):
        rows = [keyword('The Blue Moon Festival'), keyword('The Blue Moon Festival 2'),
                keyword('The Blue Moon Festival 3'), keyword('Cat')]
        matches, status, _, _ = candidates(rows, threshold=.8, max_per_keyword=1)
        self.assertEqual(status, 'COMPLETED')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].score, .8)
        self.assertEqual(matches, candidates(list(reversed(rows)), threshold=.8, max_per_keyword=1)[0])
        self.assertEqual(candidates(rows, max_batch=3)[1:3], ('SKIPPED','batch_limit'))
        self.assertEqual(candidates(rows, max_pairs=1)[1:3], ('SKIPPED','comparison_limit'))
        self.assertEqual(candidates(rows, max_posting=1)[1:3], ('SKIPPED','posting_limit'))
        self.assertEqual(candidates(rows, threshold=.81)[0], ())
        self.assertEqual(candidates([keyword('A'),keyword('B')])[0], ())
        with self.assertRaises(ValueError): candidates(rows, threshold=1.5)

    def test_normalization_does_not_identify_entity(self):
        a = keyword('Café Blue Moon Festival')
        b = keyword('Cafe\u0301 Blue Moon Festival', project='fr.wikipedia')
        found = candidates([a,b])[0]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].score, 1)
        self.assertNotEqual(found[0].left.keyword_id, found[0].right.keyword_id)
        plain = keyword('Cafe Blue Moon Festival', project='de.wikipedia')
        self.assertEqual(candidates([a,plain], threshold=.6)[0][0].score, .6)


class QualityTests(unittest.TestCase):
    def test_disabled_default_and_reconciliation(self):
        rows = [keyword('A'), keyword('The Blue Moon Festival')]
        result = analyze(rows, report_for(2))
        self.assertEqual([x.record for x in result.records], rows)
        self.assertEqual(set(result.report.stage_status.values()), {'SKIPPED'})
        self.assertEqual(result.candidates, ())
        self.assertEqual((result.report.raw_count,result.report.accepted_count,result.report.rejected_count,result.report.exact_duplicate_count),(2,2,0,0))
        with self.assertRaises(ValueError): analyze(rows, report_for(1))

    def test_flags_count_pairs_and_preserve_records(self):
        rows = [keyword('123'), keyword('The Blue Moon Festival'), keyword('The Blue Moon Festival 2')]
        cfg = AnalysisConfig(language_enabled=True, similarity_enabled=True, noise_enabled=True)
        result = analyze(rows, report_for(3), cfg)
        self.assertEqual([x.record for x in result.records], rows)
        self.assertEqual(result.report.candidate_pair_count, 1)
        self.assertEqual(result.report.records_with_flags, 3)
        self.assertGreater(result.report.flag_count, result.report.records_with_flags)
        self.assertIn('SYMBOLS_OR_NUMBERS_ONLY', result.report.flags_by_id)
        self.assertEqual(result.report.flags_by_id['NEAR_DUPLICATE_CANDIDATE'],2)
        self.assertEqual(result.report.raw_count, result.report.accepted_count + result.report.rejected_count + result.report.exact_duplicate_count)
        self.assertTrue(all(flag.source for item in result.records for flag in item.flags))

    def test_unavailable_empty_and_failure_fallback(self):
        report = clean_snapshot(snapshot(None, status=FetchStatus.DAY_UNAVAILABLE, rejected=None), []).report
        unavailable = analyze([], report, AnalysisConfig(language_enabled=True)).report
        self.assertIsNone(unavailable.raw_count)
        self.assertIsNone(unavailable.unknown_languages)
        self.assertIsNone(unavailable.candidate_pair_count)
        self.assertEqual(unavailable.stage_reason['language'], 'source_unavailable')
        bad = lambda _: (_ for _ in ()).throw(RuntimeError('synthetic failure'))
        data = analyze([keyword('Synthetic topic')],report_for(1),AnalysisConfig(language_enabled=True),detector=bad)
        self.assertEqual(data.report.stage_status['language'],'FAILED')
        self.assertIsNone(data.records[0].language)
        self.assertEqual(data.records[0].record.article_original,'Synthetic_topic')
        self.assertEqual(analyze([], report_for(0)).records, ())

    def test_technical_page_flag_without_rejection(self):
        title = 'Wiktionary:Main Page'
        record = keyword(title)
        result = analyze([record], report_for(1), AnalysisConfig(noise_enabled=True))
        self.assertIn('POSSIBLE_TECHNICAL_PAGE', [f.flag_id for f in result.records[0].flags])
        self.assertEqual(result.report.accepted_count, 1)
        self.assertEqual(result.records[0].record, record)

class GoldenSidecarIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sidecar_preserves_golden_reconciliation_and_records(self):
        from test_golden_contract import build_golden
        data, cleaned, _ = await build_golden()
        rows = data[0].keywords
        before = [item.to_record() for item in rows]
        result = analyze(rows, cleaned[0].report, AnalysisConfig(
            language_enabled=True, similarity_enabled=True, noise_enabled=True))
        report = result.report
        self.assertEqual((report.raw_count, report.accepted_count, report.rejected_count,
                          report.exact_duplicate_count), (13,6,6,1))
        self.assertEqual([item.record.to_record() for item in result.records], before)
        self.assertEqual([item.to_record() for item in rows], before)
