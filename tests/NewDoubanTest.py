import unittest
from unittest.mock import Mock, patch

import requests

import NewDouban as douban_module
from NewDouban import (
    DOUBAN_BASE,
    DoubanBookHtmlParser,
    DoubanBookSearcher,
    DoubanMetaRecord,
    NewDouban,
    _is_allowed_cover_url,
    proxy_douban_cover,
)
from cps.services.Metadata import MetaSourceInfo
from flask import request


class NewDoubanProviderTest(unittest.TestCase):
    def test_provider_keeps_plugin_identity(self):
        self.assertEqual("new_douban", NewDouban.__id__)
        self.assertEqual("NewDouban", NewDouban.__name__)

    def test_disabled_provider_returns_empty_list(self):
        provider = NewDouban()
        provider.set_status(False)
        self.assertEqual([], provider.search("test"))


class SearchTest(unittest.TestCase):
    def test_calc_url_accepts_redirect_and_direct_subject_urls(self):
        direct = "https://book.douban.com/subject/123456/"
        redirect = "https://www.douban.com/link2/?url=" + direct.replace(":", "%3A").replace("/", "%2F")
        self.assertEqual(direct, DoubanBookSearcher.calc_url(direct))
        self.assertEqual(direct, DoubanBookSearcher.calc_url(redirect))

    @patch.object(douban_module, "_request")
    def test_html_search_extracts_redirect_and_onclick_ids(self, request_mock):
        response = Mock(status_code=200)
        response.content = b"""
            <html><body>
              <a class="nbg" href="https://www.douban.com/link2/?url=https%3A%2F%2Fbook.douban.com%2Fsubject%2F111%2F"></a>
              <a class="nbg" href="#" onclick="moreurl(this, {sid: 222, type: 'b'})"></a>
            </body></html>
        """
        request_mock.return_value = response

        urls = DoubanBookSearcher().load_book_urls_from_html("query")

        self.assertEqual([
            DOUBAN_BASE + "subject/111/",
            DOUBAN_BASE + "subject/222/",
        ], urls)

    @patch.object(douban_module, "_request")
    def test_json_search_extracts_ids_and_deduplicates(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {
            "items": [
                "moreurl(this, {sid: 333, type: 'b'})",
                "moreurl(this, {sid: 333, type: 'b'})",
                "moreurl(this, {sid: 444, type: 'b'})",
            ]
        }
        request_mock.return_value = response

        urls = DoubanBookSearcher.load_book_urls_from_json("query")

        self.assertEqual([
            DOUBAN_BASE + "subject/333/",
            DOUBAN_BASE + "subject/444/",
        ], urls)

    def test_search_falls_back_to_json_results(self):
        searcher = DoubanBookSearcher()
        book = object()
        searcher.load_book_urls_from_html = Mock(return_value=[])
        searcher.load_book_urls_from_json = Mock(return_value=[DOUBAN_BASE + "subject/555/"])
        searcher.book_loader.load_book = Mock(return_value=book)

        self.assertEqual([book], searcher.search_books("query"))
        searcher.load_book_urls_from_json.assert_called_once_with("query")

    def test_search_falls_back_to_json_after_html_request_error(self):
        searcher = DoubanBookSearcher()
        book = object()
        searcher.load_book_urls_from_html = Mock(side_effect=requests.ConnectionError("offline"))
        searcher.load_book_urls_from_json = Mock(return_value=[DOUBAN_BASE + "subject/555/"])
        searcher.book_loader.load_book = Mock(return_value=book)

        self.assertEqual([book], searcher.search_books("query"))

    def test_one_bad_book_does_not_discard_other_results(self):
        searcher = DoubanBookSearcher()
        good_book = object()
        searcher.load_book_urls_from_html = Mock(return_value=[
            DOUBAN_BASE + "subject/bad/",
            DOUBAN_BASE + "subject/good/",
        ])

        def load_book(url, generic_cover):
            if "/bad/" in url:
                raise ValueError("malformed page")
            return good_book

        searcher.book_loader.load_book = load_book
        self.assertEqual([good_book], searcher.search_books("query"))


class ParserTest(unittest.TestCase):
    BOOK_HTML = """
        <html><body>
          <span property="v:itemreviewed">测试书</span>
          <a data-url="https://book.douban.com/subject/123/">分享</a>
          <a class="nbg" href="https://img1.doubanio.com/view/subject/l/public/s123.jpg"></a>
          <strong property="v:average">8.7</strong>
          <div id="info">
            <span><span class="pl">作者:</span><a href="/author/1">作者甲</a></span><br/>
            <span class="pl">出版社:</span> 测试出版社<br/>
            <span class="pl">副标题:</span> 一个副标题<br/>
            <span class="pl">出版年:</span> 2024-7<br/>
            <span class="pl">ISBN:</span> 9781234567890<br/>
          </div>
          <div id="link-report"><div class="intro"><p>内容简介</p></div></div>
          <a class="tag">技术</a>
        </body></html>
    """

    def test_parser_preserves_metadata_contract(self):
        book = DoubanBookHtmlParser().parse_book(
            DOUBAN_BASE + "subject/123/",
            self.BOOK_HTML,
        )

        self.assertEqual("123", book.id)
        self.assertEqual("测试书:一个副标题", book.title)
        self.assertEqual(["作者甲"], book.authors)
        self.assertEqual("测试出版社", book.publisher)
        self.assertEqual("2024-07-01", book.publishedDate)
        self.assertEqual({
            "douban": "123",
            "isbn": "9781234567890",
        }, book.identifiers)
        self.assertEqual(4.35, book.rating)
        self.assertEqual(["技术"], book.tags)
        self.assertIn("内容简介", book.description)

    def test_tag_fallback_uses_only_captured_criteria(self):
        tags = DoubanBookHtmlParser().get_tags(
            "criteria = '7:历史|7:文学|7:java|7:Java|8:忽略'"
        )
        self.assertEqual(["历史", "文学", "java"], tags)

    def test_page_tags_are_deduplicated_case_insensitively(self):
        html = """
            <html><body>
              <span property="v:itemreviewed">测试书</span>
              <a data-url="https://book.douban.com/subject/456/">分享</a>
              <a class="tag">Java</a>
              <a class="tag">java</a>
              <a class="tag">JAVA</a>
              <a class="tag"> 编程 </a>
            </body></html>
        """

        book = DoubanBookHtmlParser().parse_book(
            DOUBAN_BASE + "subject/456/",
            html,
        )

        self.assertEqual(["Java", "编程"], book.tags)

    def test_date_normalization(self):
        parser = DoubanBookHtmlParser()
        self.assertEqual("2024-01-01", parser.get_publish_date("2024"))
        self.assertEqual("2024-07-01", parser.get_publish_date("2024年7月"))
        self.assertEqual("2024-07-09", parser.get_publish_date("2024/7/9"))


class CoverProxyTest(unittest.TestCase):
    def setUp(self):
        request.args = {}
        request.host_url = "http://calibre.local/"

    def test_cover_record_returns_proxy_without_mutating_original(self):
        original = "https://img1.doubanio.com/view/subject/l/public/s123.jpg"
        record = DoubanMetaRecord(
            id="1",
            title="book",
            authors=[],
            url="https://book.douban.com/subject/1/",
            source=MetaSourceInfo(id="new_douban", description="test", link=DOUBAN_BASE),
            cover=original,
        )

        first = record.cover
        second = record.cover

        self.assertEqual(first, second)
        self.assertIn("metadata/douban_cover?cover=", first)
        self.assertEqual(original, object.__getattribute__(record, "cover"))

    def test_cover_allowlist_rejects_lookalikes_and_private_urls(self):
        self.assertTrue(_is_allowed_cover_url("https://img1.doubanio.com/a.jpg"))
        self.assertFalse(_is_allowed_cover_url("https://doubanio.com.example.org/a.jpg"))
        self.assertFalse(_is_allowed_cover_url("http://127.0.0.1/a.jpg"))
        self.assertFalse(_is_allowed_cover_url("file:///etc/passwd"))

    def test_proxy_rejects_non_douban_url_without_requesting_it(self):
        request.args = {"cover": "http://127.0.0.1/private"}
        with patch.object(douban_module, "_request") as request_mock:
            response = proxy_douban_cover()
        self.assertEqual(400, response.status_code)
        request_mock.assert_not_called()

    @patch.object(douban_module, "_request")
    def test_proxy_streams_successful_cover_and_closes_response(self, request_mock):
        upstream = Mock(status_code=200, headers={"Content-Type": "image/jpeg"})
        upstream.iter_content.return_value = iter([b"abc", b"", b"def"])
        request_mock.return_value = upstream
        request.args = {"cover": "https://img1.doubanio.com/a.jpg"}

        response = proxy_douban_cover()
        content = b"".join(response.response)

        self.assertEqual(b"abcdef", content)
        self.assertEqual("image/jpeg", response.content_type)
        upstream.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
