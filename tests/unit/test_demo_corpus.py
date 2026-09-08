"""The small demo remains provenance-safe without loading regression data."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from graphrag_prod.playground.demo_corpus import load_demo_corpus
from graphrag_prod.playground.runtime import PlaygroundCatalog
from scripts.playground_reset_store import CachedFixtureEmbedder, reset_playground_corpus
from graphrag_prod.domain.ids import embedding_space_id


class DemoCorpusTests(unittest.TestCase):
    def test_deterministic_exact_ranges_and_bounded_chinese_sources(self):
        fixture = load_demo_corpus()
        self.assertEqual(fixture, load_demo_corpus())
        self.assertEqual(len(fixture.plans), 4)
        chunks = [b.chunk for p in fixture.plans for b in p.bundles]
        self.assertEqual(len(chunks), 6)
        self.assertLess(sum(len(c.text) for c in chunks), 600)
        self.assertEqual({c.tenant_id for c in chunks}, {"demo-a", "demo-b"})
        for plan in fixture.plans:
            self.assertEqual(plan.bundles[0].version.language, "zh")
            self.assertEqual(''.join(b.chunk.text for b in plan.bundles), plan.bundles[0].version.normalized_text)
            for bundle in plan.bundles:
                c = bundle.chunk
                self.assertEqual(c.text, bundle.version.normalized_text[c.char_start:c.char_end])
                self.assertIsNone(bundle.embedding)
                self.assertFalse(bundle.all_assertions)

    def test_personas_keep_separate_duties_and_cross_tenant_boundary(self):
        catalog = PlaygroundCatalog(load_demo_corpus(), b'x' * 32)
        payload = catalog.bootstrap()
        self.assertEqual(len(payload['questions']), 10)
        self.assertFalse(payload['capabilities']['answer_generation'])
        roles = {frozenset(p.groups): p for p in catalog.personas if p.tenant_id == 'demo-a'}
        upload = roles[frozenset({'public', 'maintenance'})]
        review = roles[frozenset({'public', 'reviewer'})]
        public = roles[frozenset({'public'})]
        self.assertIn('knowledge:construct', upload.scopes)
        self.assertNotIn('knowledge:review', upload.scopes)
        self.assertIn('knowledge:review', review.scopes)
        self.assertNotIn('knowledge:publish', review.scopes)
        self.assertNotIn('knowledge:construct', public.scopes)
        for q in catalog.fixture.build.questions:
            self.assertFalse(set(q['relevance']).intersection(q['forbidden_chunk_ids']))
        self.assertNotIn('finance', str(payload))
        self.assertNotIn('legal', str(payload))

    def test_reset_rejects_partial_or_changed_seed_before_deletion(self):
        fixture = load_demo_corpus()
        chunks = tuple(b.chunk for p in fixture.plans for b in p.bundles)
        cached = CachedFixtureEmbedder(
            provider='test', model='test', revision='v1', dimensions=4, normalization='l2',
            embedding_space_id=embedding_space_id('test', 'test', 'v1', 4, 'l2'),
            chunks=chunks, vectors={c.text: (1., 0., 0., 0.) for c in chunks},
            dataset_id='demo-mini-zh-v1',
        )
        with self.assertRaises(ValueError):
            replace(cached, chunks=chunks[:-1])
        with self.assertRaises(ValueError):
            replace(cached, dataset_id='dev-corpus-v1')
        driver = Mock()
        with patch('scripts.run_playground._load_corpus') as load, patch('scripts.run_playground._reuse_corpus') as reuse:
            reset_playground_corpus(driver, 'neo4j', cached)
        load.assert_called_once_with(driver, 'neo4j', cached, corpus_profile='demo-mini-zh-v1')
        reuse.assert_called_once_with(driver, 'neo4j', cached, corpus_profile='demo-mini-zh-v1')

    def test_mini_selection_does_not_open_the_large_corpus(self):
        from scripts.run_playground import _corpus_fixture
        with patch('scripts.run_playground.load_dev_corpus_fixture', side_effect=AssertionError('isolated')):
            self.assertEqual(_corpus_fixture('demo-mini-zh-v1').build.manifest['counts']['active_chunks'], 6)
        with self.assertRaises(ValueError):
            _corpus_fixture('unknown')

    def test_corrupt_source_and_incomplete_ranges_fail_closed(self):
        from graphrag_prod.playground.demo_corpus import DIRECTORY
        for corruption in ('source', 'ranges'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'corpus'
                shutil.copytree(DIRECTORY, target)
                if corruption == 'source':
                    (target / 'pump-guide.txt').write_text('untrusted replacement')
                else:
                    manifest = json.loads((target / 'manifest.json').read_text())
                    manifest['documents'][0]['chunk_lengths'][0] -= 1
                    (target / 'manifest.json').write_text(json.dumps(manifest))
                with patch('graphrag_prod.playground.demo_corpus.DIRECTORY', target):
                    with self.assertRaises(ValueError):
                        load_demo_corpus()

    def test_isolation_inventory_keeps_existing_files_out_of_active_sources(self):
        root = Path(__file__).parents[2]
        inventory = json.loads((root / 'docs/playground-demo-isolation.v1.json').read_text())
        excluded = set(inventory['excluded_files'])
        active = set(inventory['active_sources'] + inventory['manual_construction_sources'])
        self.assertFalse(excluded.intersection(active))
        dataset_files = {
            str(p.relative_to(root))
            for p in (root / 'datasets').rglob('*')
            if p.is_file() and p.name != '.DS_Store'
        }
        self.assertEqual(excluded, dataset_files)
        self.assertTrue(any(p.startswith('datasets/industrial-v1/') for p in excluded))
        self.assertEqual(len(inventory['active_sources']), 4)
        for filename in excluded | active:
            self.assertTrue((root / filename).is_file(), filename)
