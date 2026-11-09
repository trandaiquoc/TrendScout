"""Offline layered pipeline regression; synthetic observations only."""
from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch

from app.models.observations import FetchStatus
from app.processing.contracts import PIPELINE_VERSION, ProcessingContext
from app.processing.pipeline import exact_dedup, process_snapshot
from app.processing.wikimedia_cleaning import clean_snapshot
from app.sources.wikimedia.cleaning_rules import RULE_VERSION
from test_wikimedia_cleaning import row, snapshot


class LayeredCleaningTests(unittest.TestCase):
    def test_version_and_count_with_reason_preservation(self):
        rows = [row('Cat'), row('Cat'), row('Main_Page'), row('Zero',views_ceil=0), row('Missing',views_ceil=None)]
        result = clean_snapshot(snapshot(5), rows)
        report = result.report
        self.assertEqual((report.raw_count, report.accepted_count, report.rejected_count, report.duplicate_count), (5,2,2,1))
        self.assertEqual(report.raw_count, report.accepted_count + report.rejected_count + report.duplicate_count)
        self.assertEqual(report.rejection_reasons, {'TECHNICAL_MAIN_PAGE':1,'MISSING_VIEWS':1})
        self.assertEqual(result.duplicates, (rows[1],))
        self.assertEqual(result.rejected[0].observation, rows[2])
        self.assertEqual(report.pipeline_version, PIPELINE_VERSION)
        self.assertEqual(RULE_VERSION, 'wikimedia-technical-pages-v0.1')

    def test_source_rules_do_not_apply_to_other_source(self):
        foreign = replace(row('Main_Page'), source='synthetic_other')
        snap = replace(snapshot(1), source='synthetic_other')
        self.assertEqual(clean_snapshot(snap, [foreign]).accepted[0].canonical_keyword, 'Main Page')
        self.assertEqual(clean_snapshot(snapshot(1), [row('Main_Page')]).report.rejected_count, 1)

    def test_unicode_and_provenance_no_second_decode(self):
        rows = [row('Vi%E1%BB%87t_Nam'), row('Café'), row('%2520')]
        accepted = clean_snapshot(snapshot(3), rows).accepted
        self.assertEqual([x.canonical_keyword for x in accepted], ['Việt Nam', 'Café', '%20'])
        for original, result in zip(rows, accepted):
            for field in ('source','country_scope','access','project','article_original','observation_date','provider_rank','views_ceil','raw_record','source_url'):
                self.assertEqual(getattr(original, field), getattr(result, field))

    def test_dedup_identity_scope_and_date(self):
        base = row('Cat')
        context = ProcessingContext(snapshot(1))
        self.assertFalse(exact_dedup(base, context).duplicate)
        self.assertTrue(exact_dedup(base, context).duplicate)
        for changed in (replace(base, source='another'), replace(base, project='fr.wikipedia'),
                        replace(base, observation_date=date(2026, 11, 5)), replace(base, country_scope='CA')):
            self.assertFalse(exact_dedup(changed, context).duplicate)

    def test_current_known_technical_page_gap_without_changing_rules(self):
        titles = ['Wikifunctions:Main_Page','Wiktionary:Main_Page','Wikisource:Main_Page',
                  'Main_Pageant','Special_Relativity']
        result = clean_snapshot(snapshot(len(titles)), [row(title) for title in titles])
        self.assertEqual([x.article_original for x in result.accepted], titles)
        self.assertEqual(result.report.rejected_count, 0)

    def test_empty_unavailable_and_stage_failure_are_not_silent(self):
        self.assertEqual(clean_snapshot(snapshot(0), []).report.accepted_count, 0)
        for status in (FetchStatus.DAY_UNAVAILABLE, FetchStatus.FETCH_ERROR):
            report = clean_snapshot(snapshot(None, status=status, rejected=None), []).report
            self.assertIsNone(report.raw_count)
            self.assertIsNone(report.accepted_count)
        with patch('app.processing.pipeline.normalization', side_effect=RuntimeError('synthetic stage failure')):
            with self.assertRaisesRegex(RuntimeError, 'stage failure'):
                clean_snapshot(snapshot(1), [row()])
