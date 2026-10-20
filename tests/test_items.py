import re
import unittest

import support  # noqa: F401
from newsradar.items import AI_TERMS, canonical_url, clean, digest, is_web_url, item


class CleanTests(unittest.TestCase):
    def test_strips_tags_decodes_entities_and_collapses_space(self):
        self.assertEqual(clean("<p>Hello&nbsp;<b>AI</b>\n\t world &amp; co</p>"), "Hello AI world & co")

    def test_escaped_markup_is_decoded_after_tag_removal(self):
        # Order matters: escaped tags survive as text, they are content not markup.
        self.assertEqual(clean("&lt;b&gt;bold&lt;/b&gt;"), "<b>bold</b>")

    def test_none_and_empty(self):
        self.assertEqual(clean(None), "")
        self.assertEqual(clean("   "), "")

    def test_result_has_no_padding_or_double_spaces(self):
        text = clean("  a \n\n b  c  ")
        self.assertEqual(text, "a b c")


class DigestTests(unittest.TestCase):
    def test_sha256_hex(self):
        self.assertEqual(digest(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(len(digest("가나다")), 64)


class CanonicalUrlTests(unittest.TestCase):
    def test_drops_tracking_parameters_case_insensitively(self):
        self.assertEqual(canonical_url("https://a.test/x?UTM_Source=a&utm_medium=b&fbclid=1&gclid=2&mc_cid=3&mc_eid=4&id=7"),
                         "https://a.test/x?id=7")

    def test_keeps_other_parameters_in_order_with_blank_values(self):
        self.assertEqual(canonical_url("https://a.test/x?b=2&a=&a=1"), "https://a.test/x?b=2&a=&a=1")

    def test_lowercases_scheme_and_host_but_not_path(self):
        self.assertEqual(canonical_url("HTTPS://A.Test:8443/Path/Case"), "https://a.test:8443/Path/Case")

    def test_trailing_slashes_and_root(self):
        self.assertEqual(canonical_url("https://a.test/news///"), "https://a.test/news")
        self.assertEqual(canonical_url("https://a.test"), "https://a.test/")
        self.assertEqual(canonical_url("https://a.test/"), "https://a.test/")

    def test_keeps_fragment_and_trims_whitespace(self):
        self.assertEqual(canonical_url("  https://a.test/x/#top \n"), "https://a.test/x#top")

    def test_requery_encoding_is_normalised(self):
        self.assertEqual(canonical_url("https://a.test/s?q=a%20b"), "https://a.test/s?q=a+b")

    def test_is_idempotent(self):
        for url in ("https://A.test/a/?utm_x=1&q=a%20b#f", "http://a.test", "https://a.test/%7Euser/?x=1&x=2"):
            once = canonical_url(url)
            self.assertEqual(canonical_url(once), once)

    def test_is_web_url(self):
        self.assertTrue(is_web_url("https://a.test/"))
        self.assertTrue(is_web_url("http://a.test/"))
        for url in ("ftp://a.test/", "javascript:alert(1)", "mailto:x@a.test", "/relative", ""):
            self.assertFalse(is_web_url(url), url)


class AiTermsTests(unittest.TestCase):
    POSITIVE = ["New AI model", "GPT-4o launch", "ChatGPT update", "A.I. rules", "a.i. ethics", "an AGI claim", "Hugging Face hub",
                "HuggingFace", "LoRA adapters", "RAG pipeline", "fine-tuning guide", "text-to-video", "VLMs compared",
                "robotics lab", "NVIDIA earnings", "CUDA 13", "Claude Code", "Gemini 3", "DeepSeek R2", "vLLM release",
                "인공지능 정책", "생성형 서비스", "에이전트 플랫폼", "大模型发布", "生成ＡＩの活用", "大規模言語モデル", "智谱", "通义千问",
                "multimodal search", "retrieval-augmented generation", "machine learning", "deep learning"]
    NEGATIVE = ["chair rail retailer", "said the mayor", "MAIN street", "trained staff", "Cairo travel", "rags to riches",
                "aim higher", "email", "the airline", "pAInt"]

    def test_positives(self):
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertRegex(text, AI_TERMS)

    def test_negatives_respect_word_boundaries(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertIsNone(AI_TERMS.search(text))

    def test_case_insensitive(self):
        self.assertTrue(AI_TERMS.search("openai and pytorch"))
        self.assertTrue(re.search(AI_TERMS, "LARGE LANGUAGE models"))


class ItemTests(unittest.TestCase):
    def test_record_fields(self):
        record = item("k", " <b>Title</b> ", "https://a.test/x", "<p>Body</p>", "2026-10-19T00:00:00Z", "published")
        self.assertEqual(record["key"], "k")
        self.assertEqual(record["title"], "Title")
        self.assertEqual(record["url"], "https://a.test/x")
        self.assertEqual(record["content_hash"], digest("Body"))
        self.assertEqual(record["precision"], "instant")
        self.assertEqual(record["source_time_raw"], "2026-10-19T00:00:00Z")
        self.assertEqual(record["basis"], "published")

    def test_title_is_truncated_to_300_characters_after_cleaning(self):
        self.assertEqual(len(item("k", "x" * 500, "https://a.test")["title"]), 300)

    def test_hash_ignores_markup_and_whitespace_only_changes(self):
        a = item("k", "T", "u", "<p>Same   body</p>")
        b = item("k", "T", "u", "Same\nbody")
        self.assertEqual(a["content_hash"], b["content_hash"])

    def test_ai_match_uses_title_body_and_url_of_this_entry_only(self):
        self.assertTrue(item("k", "Weather", "https://a.test/x", "A large language model")["ai_match"])
        self.assertTrue(item("k", "Weather", "https://openai.com/x", "rain")["ai_match"])
        self.assertFalse(item("k", "Weather", "https://a.test/x", "rain")["ai_match"])

    def test_defaults(self):
        record = item("k", None, "u")
        self.assertEqual(record["title"], "")
        self.assertEqual((record["source_time"], record["precision"], record["basis"]), (None, "unknown", "unknown"))


if __name__ == "__main__":
    unittest.main()
