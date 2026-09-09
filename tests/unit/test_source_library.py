"""Read authorization, source integrity and bounded source library contracts."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
import unittest
from graphrag_prod.domain import Principal
from graphrag_prod.graph.browse_models import GraphViewChanged
from graphrag_prod.knowledge.source_library import Neo4jSourceLibrary, _BOUNDARY
from graphrag_prod.api.source_contracts import SourceListRequest, SourceReadRequest

class SourceLibraryTests(unittest.TestCase):
    def test_source_requests_reject_identity_injection_and_unbounded_reads(self):
        for value in ({'limit':101},{'limit':True},{'tenant_id':'other'}):
            with self.assertRaises(ValueError): SourceListRequest.model_validate(value)
        with self.assertRaises(ValueError): SourceReadRequest(document_id='d',version_id='v',ordinal=-1)
        reader=Neo4jSourceLibrary(None)
        with self.assertRaises(PermissionError): reader.read(Principal('p','tenant',frozenset({'g'})))

    def test_source_projection_checks_exact_checksum_and_rechecks_visibility(self):
        row=dict(text='😀原文',char_start=0,char_end=3,checksum=sha256('😀原文'.encode()).hexdigest())
        class Tx:
            calls=0
            def run(self, query, **params):
                self.calls+=1
                return [deepcopy(row)]
        tx=Tx();self.assertEqual(Neo4jSourceLibrary._read_tx(tx,{},True,30)['text'],'😀原文');self.assertEqual(tx.calls,2)
        row['checksum']='a'*64
        with self.assertRaises(GraphViewChanged): Neo4jSourceLibrary._read_tx(Tx(),{},True,30)
        class Revoked:
            calls=0
            def run(self, query, **params):
                self.calls+=1
                return [{'document_id':'d'}] if self.calls==1 else []
        with self.assertRaises(GraphViewChanged): Neo4jSourceLibrary._read_tx(Revoked(),{},False,30)

    def test_list_has_real_continuation_and_human_sources_keep_publication_gate(self):
        tx=SimpleNamespace(run=lambda *args,**kwargs:[{'document_id':'a'},{'document_id':'b'}])
        self.assertEqual(Neo4jSourceLibrary._read_tx(tx,{},False,1),{'items':[{'document_id':'a'}],'has_more':True,'next_after':'a'})
        for invariant in ('ACTIVE_SNAPSHOT','ACTIVE_VERSION','CURRENT_REVISION','PUBLISHES_KNOWLEDGE_REVISION',"hr.authority_level='SECONDARY'",'hr.access_groups','member.access_policy_version'):
            self.assertIn(invariant,_BOUNDARY)
