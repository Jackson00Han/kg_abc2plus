"""A slow answer cannot return evidence from an obsolete publication view."""
from types import SimpleNamespace
import unittest

from graphrag_prod.api.backend import GraphRAGQueryOperations, GeneratedAnswer, ProviderUsage
from graphrag_prod.api.contracts import AnswerRequest
from graphrag_prod.api.runtime import GraphViewChangedError, UsageMetadata
from graphrag_prod.domain import Principal
from graphrag_prod.generation import AnswerResult
from graphrag_prod.graph.browse_models import GraphViewChanged


class AnswerPublicationPinTests(unittest.TestCase):
    def build(self, *, change_at=None):
        events=[]
        principal=Principal('reader','pump-answer-test',frozenset({'members'}))
        result=SimpleNamespace(chunks=(),trace=SimpleNamespace(
            knowledge_publication_id='pump-release-v1',knowledge_publication_generation=1,
            knowledge_activation_generation=3))
        def validate(actual_principal, actual_result):
            self.assertEqual(actual_principal,principal)
            self.assertIs(actual_result,result)
            events.append('validate')
            if events.count('validate')==change_at:
                raise GraphViewChanged()
        def generate(request):
            events.append('generate')
            return GeneratedAnswer(AnswerResult.refusal(),ProviderUsage())
        operations=object.__new__(GraphRAGQueryOperations)
        operations._retrieval_engine=SimpleNamespace(validate_result=validate)
        operations._retrieve=lambda *args,**kwargs:(result,UsageMetadata())
        operations._monotonic=lambda:0.0
        operations._generate=generate
        return operations,principal,events

    def test_answer_returns_its_publication_identity_after_two_checks(self):
        operations,principal,events=self.build()
        response=operations.answer(principal,AnswerRequest(query_text='循环水泵额定功率是多少？'))
        self.assertEqual(events,['validate','generate','validate'])
        self.assertEqual(response.payload.knowledge_publication_id,'pump-release-v1')
        self.assertEqual(response.payload.knowledge_activation_generation,3)

    def test_switch_before_generation_prevents_model_access(self):
        operations,principal,events=self.build(change_at=1)
        with self.assertRaises(GraphViewChangedError):
            operations.answer(principal,AnswerRequest(query_text='循环水泵额定功率是多少？'))
        self.assertEqual(events,['validate'])

    def test_switch_or_revocation_during_generation_discards_answer(self):
        operations,principal,events=self.build(change_at=2)
        with self.assertRaises(GraphViewChangedError):
            operations.answer(principal,AnswerRequest(query_text='循环水泵额定功率是多少？'))
        self.assertEqual(events,['validate','generate','validate'])
