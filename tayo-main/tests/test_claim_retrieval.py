from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.app import main
from backend.app.schemas import FactCheckRequest, UrlCheckRequest
from backend.app.services.claim_retrieval import (
    _aggregate_verdict,
    _medical_stance,
    _relevance,
    _source_topics,
    rating_to_verdict,
    source_catalog,
)
from backend.app.services.claim_understanding import (
    analyze_claim,
    clean_claim_text,
    find_concepts,
)
from backend.app.services.trusted_sources import (
    list_sources,
    load_registry,
    source_search_links,
)


class ClaimUnderstandingTests(unittest.TestCase):
    def test_honey_cancer_claim_is_read_as_a_medical_treatment_claim(self):
        result = analyze_claim(
            "Зөгийн балыг тогтмол идэх нь хорт хавдрыг шууд эдгээнэ"
        )
        keys = [concept["key"] for concept in result["concepts"]]
        self.assertIn("honey", keys)
        self.assertIn("cancer", keys)
        self.assertEqual(result["domain"], "public_health")
        self.assertEqual(result["effective_scope"], "medical")
        self.assertIn("treatment", result["claim_forms"])
        self.assertTrue(
            any(query["purpose"] == "medical" for query in result["queries"])
        )

    def test_blood_first_aid_claim_is_not_treated_as_general_chat(self):
        result = analyze_claim("Цус гарч байгаа үед цусаа сорох нь зүгээр")
        keys = [concept["key"] for concept in result["concepts"]]
        self.assertIn("blood", keys)
        self.assertIn("suction", keys)
        self.assertEqual(result["effective_scope"], "medical")
        self.assertIn("recommendation", result["claim_forms"])

    def test_mongolian_case_suffixes_match_concepts(self):
        for text, expected in (
            ("хавдрыг", "cancer"),
            ("хавдрын", "cancer"),
            ("балыг", "honey"),
            ("балын", "honey"),
            ("цусаа", "blood"),
            ("усны", "water"),
        ):
            with self.subTest(text=text):
                self.assertIn(
                    expected,
                    [concept["key"] for concept in find_concepts(text)],
                )

    def test_copied_social_framing_is_removed(self):
        self.assertEqual(
            clean_claim_text(
                "Facebook дээр бичжээ: “Ковидын вакцин ДНХ-г өөрчилдөг” гэж. "
                "Та үүнд итгэж байна уу?"
            ),
            "Ковидын вакцин ДНХ-г өөрчилдөг",
        )
        self.assertEqual(
            clean_claim_text(
                'Twitter-ээс: "Vaccines cause autism" Share this before it gets deleted'
            ),
            "Vaccines cause autism",
        )

    def test_queries_are_concepts_not_the_whole_sentence(self):
        claim = "Зөгийн балыг тогтмол идэх нь хорт хавдрыг шууд эдгээнэ"
        result = analyze_claim(claim)
        queries = [query["query"] for query in result["queries"]]
        self.assertTrue(queries)
        self.assertNotIn(claim, queries)
        self.assertTrue(any("зөгийн бал" in query for query in queries))
        self.assertTrue(any("honey" in query for query in queries))


class RetrievalVerdictTests(unittest.TestCase):
    def test_publisher_ratings_map_conservatively(self):
        self.assertEqual(rating_to_verdict("Mostly False"), "False")
        self.assertEqual(rating_to_verdict("Худал"), "False")
        self.assertEqual(rating_to_verdict("Үнэн"), "True")
        self.assertEqual(rating_to_verdict("Partly true"), "Not Sure")
        self.assertEqual(rating_to_verdict(""), "Not Sure")

    def test_medical_stance_needs_a_title_level_refutation(self):
        self.assertEqual(
            _medical_stance(
                "Honey does not cure cancer",
                "The study found no evidence of a cure.",
                2,
            ),
            "contradicts",
        )
        # A generic "no evidence" sentence in the abstract is not enough:
        # abstracts often say this about an unrelated subgroup.
        self.assertEqual(
            _medical_stance(
                "Honey and cancer survival",
                "No evidence of harm was found in this subgroup.",
                2,
            ),
            "context",
        )
        self.assertEqual(
            _medical_stance("Aspiration for retained products", "Context.", 2),
            "context",
        )

    def test_broad_concepts_alone_do_not_unlock_a_published_verdict(self):
        analysis = analyze_claim(
            "Pfizer-ийн COVID-19 вакцины сөрөг нөлөөний нууц жагсаалт ил болсон"
        )
        relevance, hits, specific = _relevance(
            analysis, "COVID vaccines cause infertility"
        )
        self.assertEqual(hits, 2)
        self.assertEqual(specific, 0)
        self.assertLess(relevance, 0.60)
        verdict, _, basis, reported = _aggregate_verdict(
            analysis,
            [
                {
                    "published_verdict": "False",
                    "relevance": relevance,
                    "concept_hits": hits,
                    "specific_hits": specific,
                    "title": "Unrelated vaccine review",
                }
            ],
            [],
        )
        self.assertEqual(verdict, "Not Sure")
        self.assertEqual(basis, "no_verdict")
        self.assertIsNone(reported)

    def test_a_specific_concept_keeps_a_close_fact_check_usable(self):
        analysis = analyze_claim("Вакцин аутизм үүсгэдэг.")
        relevance, hits, specific = _relevance(analysis, "Vaccines cause autism")
        self.assertGreaterEqual(relevance, 0.60)
        self.assertEqual((hits, specific), (2, 1))

    def test_broad_only_concept_hits_cannot_make_medical_literature_contradict(self):
        self.assertEqual(
            _medical_stance(
                "COVID vaccines do not cause infertility",
                "No evidence was found.",
                2,
                0,
            ),
            "context",
        )
        self.assertEqual(
            _medical_stance(
                "Honey does not cure cancer",
                "No evidence was found.",
                2,
                2,
            ),
            "contradicts",
        )

    def test_opposite_polarity_review_does_not_set_a_verdict(self):
        analysis = analyze_claim("Вакцин аутизм үүсгэдэггүй.")
        verdict, _, basis, reported = _aggregate_verdict(
            analysis,
            [
                {
                    "published_verdict": "False",
                    "relevance": 0.75,
                    "concept_hits": 2,
                    "specific_hits": 1,
                    "polarity_mismatch": True,
                    "title": "Vaccines cause autism",
                }
            ],
            [],
        )
        self.assertEqual(verdict, "Not Sure")
        self.assertEqual(basis, "no_verdict")
        self.assertIsNone(reported)

    def test_negated_claim_does_not_adopt_positive_review(self):
        # The same fact-check wording ("no evidence") that contradicts the
        # positive claim cannot be allowed to contradict its negation too.
        analysis = analyze_claim("Вакцин аутизм үүсгэдэггүй.")
        verdict, _, basis, _ = _aggregate_verdict(
            analysis,
            [],
            [
                {
                    "relation": "contradicts",
                    "concept_hits": 2,
                    "specific_hits": 1,
                    "title": "No evidence that vaccines cause autism",
                },
                {
                    "relation": "contradicts",
                    "concept_hits": 2,
                    "specific_hits": 1,
                    "title": "Vaccines are not linked to autism",
                },
            ],
        )
        self.assertEqual(verdict, "Not Sure")
        self.assertEqual(basis, "no_verdict")

    def test_meta_claim_review_is_not_transferred(self):
        analysis = analyze_claim("Вакцин аутизм үүсгэдэггүй.")
        verdict, _, basis, reported = _aggregate_verdict(
            analysis,
            [
                {
                    "published_verdict": "False",
                    "relevance": 0.75,
                    "concept_hits": 2,
                    "specific_hits": 1,
                    "polarity_mismatch": False,
                    "meta_claim": True,
                    "title": "CDC edits website to include false claims",
                }
            ],
            [],
        )
        self.assertEqual(verdict, "Not Sure")
        self.assertEqual(basis, "no_verdict")
        self.assertIsNone(reported)

    def test_two_explicit_medical_contradictions_can_lower_the_verdict(self):
        verdict, confidence, basis, _ = _aggregate_verdict(
            {},
            [],
            [
                {
                    "relation": "contradicts",
                    "concept_hits": 2,
                    "specific_hits": 2,
                    "title": "A",
                },
                {
                    "relation": "contradicts",
                    "concept_hits": 2,
                    "specific_hits": 2,
                    "title": "B",
                },
            ],
        )
        self.assertEqual(verdict, "False")
        self.assertEqual(basis, "medical_literature")
        self.assertLess(confidence, 0.7)

    def test_context_only_literature_stays_not_sure(self):
        verdict, _, basis, _ = _aggregate_verdict(
            {},
            [],
            [
                {"relation": "context", "concept_hits": 2, "title": "A"},
                {"relation": "context", "concept_hits": 2, "title": "B"},
            ],
        )
        self.assertEqual(verdict, "Not Sure")
        self.assertEqual(basis, "no_verdict")


class RegistryTests(unittest.TestCase):
    def test_registry_is_mongolia_first_and_large_enough(self):
        sources = load_registry().get("sources") or []
        self.assertGreaterEqual(len(sources), 80)
        self.assertGreaterEqual(sum(1 for s in sources if s["language"] == "mn"), 40)
        self.assertGreaterEqual(sum(1 for s in sources if s.get("medical")), 60)
        self.assertGreaterEqual(
            sum(1 for s in sources for _ in (s.get("feeds") or [])), 8
        )
        ids = [s["id"] for s in sources]
        self.assertEqual(len(ids), len(set(ids)))

    def test_scope_filters_are_real_filters(self):
        medical = list_sources(scope="medical")
        self.assertTrue(medical)
        self.assertTrue(all(source["medical"] for source in medical))
        fact_checks = list_sources(scope="fact_check")
        self.assertTrue(fact_checks)
        self.assertTrue(all(source["category"] == "fact_check" for source in fact_checks))

    def test_source_catalog_reports_counts_and_categories(self):
        catalog = source_catalog(scope="medical")
        self.assertEqual(catalog["total"], len(catalog["sources"]))
        self.assertEqual(catalog["counts"]["total"], catalog["total"])
        self.assertEqual(catalog["counts"]["medical"], catalog["total"])
        self.assertGreaterEqual(len(catalog["categories"]), 3)

    def test_search_links_encode_the_query_and_keep_medical_scope(self):
        links = source_search_links("зөгийн бал хорт хавдар", scope="medical", limit=5)
        self.assertTrue(links)
        self.assertTrue(all(link["is_medical"] for link in links))
        self.assertEqual(links[0]["source"], "FACTCHECK.mn")
        self.assertIn("%D0%B7", links[0]["url"])

    def test_honey_cancer_links_do_not_prioritize_unrelated_agencies(self):
        claim = "Зөгийн балыг тогтмол идэх нь хорт хавдрыг шууд эдгээнэ"
        analysis = analyze_claim(claim)
        links = source_search_links(
            claim,
            scope="medical",
            limit=10,
            topics=_source_topics(analysis),
        )
        ids = [link["source_id"] for link in links]
        self.assertEqual(ids[0], "factcheck-mn")
        self.assertIn("journal-mnums-edu-mn", ids)
        for unrelated in ("ncmh-gov-mn", "nea-gov-mn", "nema-gov-mn", "nczd-gov-mn"):
            self.assertNotIn(unrelated, ids)

    def test_first_aid_claim_prioritizes_first_aid_sources(self):
        claim = "Цус гарч байгаа үед цусаа сорох нь зүгээр"
        analysis = analyze_claim(claim)
        links = source_search_links(
            claim,
            scope="medical",
            limit=10,
            topics=_source_topics(analysis),
        )
        ids = [link["source_id"] for link in links]
        self.assertIn("redcross-mn", ids)
        self.assertIn("nema-gov-mn", ids)
        self.assertLess(ids.index("redcross-mn"), ids.index("snopes"))
        self.assertIn("first_aid", links[ids.index("redcross-mn")]["matched_topics"])

    def test_nuclear_claim_prioritizes_nuclear_sources(self):
        claim = "Цөмийн цахилгаан станц аюулгүй"
        analysis = analyze_claim(claim)
        links = source_search_links(
            claim,
            scope="medical",
            limit=10,
            topics=_source_topics(analysis),
        )
        ids = [link["source_id"] for link in links]
        self.assertIn("nea-gov-mn", ids)
        self.assertIn("iaea", ids)
        self.assertNotIn("ncmh-gov-mn", ids)


class ApiCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_factcheck_route_forwards_source_scope(self):
        check = AsyncMock(return_value={"verdict": "Not Sure"})
        with patch.object(main, "factcheck_claim", new=check):
            await main.api_factcheck(
                FactCheckRequest(
                    claim="Зөгийн бал хорт хавдрыг эдгээнэ",
                    source_scope="medical",
                )
            )
        check.assert_awaited_once_with(
            "Зөгийн бал хорт хавдрыг эдгээнэ", "auto", "medical"
        )

    async def test_sources_route_returns_the_registry(self):
        data = await main.api_factcheck_sources(scope="medical")
        self.assertEqual(data["scope"], "medical")
        self.assertGreater(data["total"], 0)
        self.assertTrue(all(source["medical"] for source in data["sources"]))

    async def test_url_request_accepts_source_scope(self):
        payload = UrlCheckRequest(
            url="https://example.com/story",
            source_scope="fact_check",
        )
        self.assertEqual(payload.source_scope, "fact_check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
