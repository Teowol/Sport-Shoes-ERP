"""Real pgvector ranking with isolated inference and document authorization."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ai.models import Document
from ai.services.document_search import MAX_QUERY_CHARACTERS, MAX_SEARCH_LIMIT, search_document_chunks
from ai.services.local_embeddings import LocalEmbeddingConfigurationError, LocalEmbeddingInputError, get_embedding_profile
from ai.test_embedding_pipeline import STORAGE, UNIT_VECTOR, ready_document
from ai.tools import TOOL_FUNCTIONS, search_documents


@override_settings(STORAGES=STORAGE)
class DocumentSearchTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create(username="search-staff", is_staff=True)
        self.factory = user_model.objects.create(username="search-factory")
        self.buyer = user_model.objects.create(username="search-buyer")
        self.buyer_staff = user_model.objects.create(username="search-buyer-staff", is_staff=True)
        self.mixed = user_model.objects.create(username="search-mixed")
        self.ordinary = user_model.objects.create(username="search-ordinary")
        self.superuser = user_model.objects.create(username="search-super", is_superuser=True)
        self.inactive = user_model.objects.create(username="search-inactive", is_superuser=True, is_active=False)
        buyers = Group.objects.create(name="Buyer")
        owners = Group.objects.create(name="FactoryOwner")
        buyers.user_set.add(self.buyer, self.buyer_staff, self.mixed, self.superuser)
        owners.user_set.add(self.factory, self.mixed)
        self.profile = get_embedding_profile()
        self.service = SimpleNamespace(profile=self.profile, embed_query=Mock(return_value=UNIT_VECTOR[:]))
        self.loader = self.enterContext(patch("ai.services.document_search.get_local_embedding_service", return_value=self.service))

    def document(self, vectors, title="Kalite prosedürü", status=Document.Status.READY):
        document = ready_document(count=len(vectors))
        Document.objects.filter(pk=document.pk).update(title=title, status=status, uploaded_by=self.staff)
        for chunk, vector in zip(document.chunks.all(), vectors, strict=True):
            if vector is not None:
                document.chunks.filter(pk=chunk.pk).update(
                    embedding=vector, embedding_model=self.profile.model_id,
                    embedding_dimensions=384, embedding_profile_hash=self.profile.profile_hash,
                    embedded_at=timezone.now(), page_number=chunk.chunk_index + 1,
                    section_title=f"Bölüm {chunk.chunk_index + 1}",
                )
        return document

    def test_pgvector_cosine_ranks_and_limits_in_sql(self):
        self.document([[0.0, 1.0] + [0.0] * 382], title="İlgisiz")
        self.document([[0.8, 0.6] + [0.0] * 382], title="Yakın")
        self.document([UNIT_VECTOR], title="En yakın")
        with CaptureQueriesContext(connection) as captured:
            result = search_documents(self.staff, "  kalite kontrol  ", limit=2)
        self.assertTrue(result["ok"])
        self.assertEqual([row["document_name"] for row in result["data"]], ["En yakın", "Yakın"])
        self.assertAlmostEqual(result["data"][0]["cosine_distance"], 0.0)
        self.assertAlmostEqual(result["data"][1]["cosine_distance"], 0.2, places=6)
        self.service.embed_query.assert_called_once_with("kalite kontrol")
        ranking_sql = next(q["sql"] for q in captured if "<=>" in q["sql"])
        self.assertIn("LIMIT 2", ranking_sql)
        self.assertIn("ORDER BY", ranking_sql)

    def test_factory_staff_and_superuser_can_read_other_uploaders_documents(self):
        self.document([UNIT_VECTOR])
        for user in (self.staff, self.factory, self.superuser):
            with self.subTest(username=user.username):
                self.assertEqual(len(search_documents(user, "kalite")["data"]), 1)

    def test_denied_roles_cannot_query_chunks_or_load_model(self):
        document = self.document([UNIT_VECTOR])
        Document.objects.filter(pk=document.pk).update(uploaded_by=self.ordinary)
        for user in (AnonymousUser(), self.buyer, self.buyer_staff, self.mixed, self.ordinary, self.inactive):
            with self.subTest(user=str(user)), CaptureQueriesContext(connection) as captured:
                result = search_documents(user, "kalite")
                self.assertEqual(result["error"], "access_denied")
                self.assertEqual(result["data"], [])
            self.assertFalse(any('"ai_document' in q["sql"] for q in captured))
        self.loader.assert_not_called()

    def test_direct_service_call_cannot_bypass_document_permissions(self):
        self.document([UNIT_VECTOR])
        with self.assertRaises(PermissionDenied):
            search_document_chunks(self.buyer_staff, "kalite")
        self.loader.assert_not_called()

    def test_role_revocation_takes_effect_on_next_call(self):
        self.document([UNIT_VECTOR])
        self.assertTrue(search_documents(self.factory, "kalite")["data"])
        self.factory.groups.add(Group.objects.get(name="Buyer"))
        self.assertEqual(search_documents(self.factory, "kalite")["error"], "access_denied")
        self.assertEqual(self.service.embed_query.call_count, 1)

    def test_profile_status_and_pending_filters_apply_before_limit(self):
        for status in (Document.Status.QUEUED, Document.Status.UPLOADED, Document.Status.PROCESSING, Document.Status.FAILED):
            self.document([UNIT_VECTOR], status=status)
        self.document([None])
        wrong_hash = self.document([UNIT_VECTOR])
        wrong_hash.chunks.update(embedding_profile_hash="f" * 64)
        wrong_model = self.document([UNIT_VECTOR])
        wrong_model.chunks.update(embedding_model="other-model")
        self.document([[0.0, 1.0] + [0.0] * 382], title="Uygun belge")
        result = search_documents(self.staff, "kalite", limit=1)
        self.assertEqual([row["document_name"] for row in result["data"]], ["Uygun belge"])

    def test_no_compatible_vectors_returns_empty_without_model_loading(self):
        self.document([None])
        document = self.document([UNIT_VECTOR])
        document.chunks.update(embedding_profile_hash="f" * 64)
        self.assertEqual(search_documents(self.staff, "kalite")["data"], [])
        self.loader.assert_not_called()

    def test_payload_has_only_explicit_public_fields_and_is_json_safe(self):
        document = self.document([UNIT_VECTOR])
        result = search_documents(self.staff, "kalite")
        row = result["data"][0]
        self.assertEqual(set(row), {"source_type", "document_name", "content", "page_number", "section_title", "cosine_distance"})
        self.assertEqual(row["source_type"], "document")
        self.assertEqual(row["page_number"], 1)
        self.assertEqual(row["section_title"], "Bölüm 1")
        payload = json.dumps(result, allow_nan=False)
        for forbidden in (str(document.public_id), document.file.name, self.profile.profile_hash,
                          '"id"', '"document_id"', '"cost"', '"price"', '"uploaded_by"', '"embedding"'):
            self.assertNotIn(forbidden, payload)

    def test_missing_page_and_section_remain_uninvented(self):
        document = self.document([UNIT_VECTOR])
        document.chunks.update(page_number=None, section_title="")
        row = search_documents(self.staff, "kalite")["data"][0]
        self.assertIsNone(row["page_number"])
        self.assertEqual(row["section_title"], "")

    def test_search_executes_only_reads_and_preserves_metadata(self):
        document = self.document([UNIT_VECTOR])
        before = list(document.chunks.values())
        with CaptureQueriesContext(connection) as captured:
            self.assertTrue(search_documents(self.staff, "kalite")["ok"])
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in captured))
        self.assertEqual(list(document.chunks.values()), before)

    def test_limit_is_capped_and_ties_are_stable(self):
        document = self.document([UNIT_VECTOR] * (MAX_SEARCH_LIMIT + 2))
        first = search_documents(self.staff, "kalite", limit=1000)
        self.assertEqual(len(first["data"]), MAX_SEARCH_LIMIT)
        self.assertEqual([row["content"] for row in first["data"]], list(document.chunks.values_list("content", flat=True)[:MAX_SEARCH_LIMIT]))
        self.assertEqual(search_documents(self.staff, "kalite", limit=1000), first)
        self.assertEqual(len(search_documents(self.staff, "kalite")["data"]), 5)

    def test_invalid_input_is_rejected_before_loading_model(self):
        self.document([UNIT_VECTOR])
        for query in (None, 3, [], {}, "x" * (MAX_QUERY_CHARACTERS + 1)):
            with self.subTest(query_type=type(query).__name__):
                self.assertEqual(search_documents(self.staff, query)["error"], "invalid_tool_arguments")
        for limit in (None, True, False, 0, -1, 1.5, "3", [], {}):
            with self.subTest(limit=limit):
                self.assertEqual(search_documents(self.staff, "kalite", limit)["error"], "invalid_tool_arguments")
        self.assertEqual(search_documents(self.staff, " \n\t ")["data"], [])
        self.loader.assert_not_called()

    def test_token_limit_error_is_generic_and_does_not_search(self):
        self.document([UNIT_VECTOR])
        self.service.embed_query.side_effect = LocalEmbeddingInputError("private input detail")
        result = search_documents(self.staff, "kalite")
        self.assertEqual(result["error"], "invalid_tool_arguments")
        self.assertNotIn("private", json.dumps(result))

    def test_loaded_profile_mismatch_fails_closed(self):
        self.document([UNIT_VECTOR])
        self.service.profile = replace(self.profile, profile_hash="f" * 64)
        result = search_documents(self.staff, "kalite")
        self.assertEqual(result["error"], "tool_unavailable")
        self.assertEqual(result["data"], [])
        self.service.embed_query.assert_not_called()

    def test_profile_change_during_inference_fails_closed(self):
        self.document([UNIT_VECTOR])
        with patch("ai.services.document_search.get_embedding_profile", side_effect=[self.profile, replace(self.profile, profile_hash="f" * 64)]):
            self.assertEqual(search_documents(self.staff, "kalite")["error"], "tool_unavailable")

    def test_invalid_query_vector_fails_closed(self):
        self.document([UNIT_VECTOR])
        for vector in ([1.0], [0.0] * 384, [float("nan")] * 384, [float("inf")] * 384, [2.0] + [0.0] * 383):
            with self.subTest(first=vector[0]):
                self.service.embed_query.return_value = vector
                self.assertEqual(search_documents(self.staff, "kalite")["error"], "tool_unavailable")

    def test_zero_stored_vector_does_not_displace_valid_results(self):
        self.document([[0.0] * 384], title="Bozuk")
        self.document([UNIT_VECTOR], title="Geçerli")
        result = search_documents(self.staff, "kalite", limit=1)
        self.assertEqual([row["document_name"] for row in result["data"]], ["Geçerli"])

    def test_backend_failures_do_not_expose_paths_or_database_details(self):
        self.document([UNIT_VECTOR])
        for error in (LocalEmbeddingConfigurationError("C:/private/model"), OSError("private path")):
            with self.subTest(error=type(error).__name__):
                self.loader.side_effect = error
                result = search_documents(self.staff, "kalite")
                self.assertEqual(result["error"], "tool_unavailable")
                self.assertNotIn("private", json.dumps(result))
        with patch("ai.tools.search_document_chunks", side_effect=OperationalError("private database")):
            self.assertEqual(search_documents(self.staff, "kalite")["error"], "tool_unavailable")

    def test_assistant_registration_includes_document_search(self):
        from ai.services.assistant import LLMService
        self.assertEqual(len(TOOL_FUNCTIONS), 7)
        self.assertIs(TOOL_FUNCTIONS["search_documents"], search_documents)
        self.assertIn("search_documents", [d["function"]["name"] for d in LLMService().tool_definitions_for(self.staff)])
