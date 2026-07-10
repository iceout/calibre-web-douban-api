import dataclasses
import logging
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
from flask import Response, request
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cps import helper
from cps.search_metadata import meta
from cps.services.Metadata import Metadata, MetaRecord, MetaSourceInfo


# Whether cover URLs returned to Calibre-Web should use the local proxy.
DOUBAN_PROXY_COVER = True
# Set this when request.host_url does not point at the externally reachable server.
DOUBAN_PROXY_COVER_HOST_URL = ""
DOUBAN_PROXY_COVER_PATH = "metadata/douban_cover?cover="

DOUBAN_SEARCH_URL = "https://www.douban.com/search"
DOUBAN_SEARCH_JSON_URL = "https://www.douban.com/j/search"
DOUBAN_BASE = "https://book.douban.com/"
DOUBAN_COVER_DOMAIN = "doubanio.com"
DOUBAN_BOOK_CAT = "1001"
DOUBAN_BOOK_CACHE_SIZE = 500
DOUBAN_CONCURRENCY_SIZE = 5
DOUBAN_REQUEST_TIMEOUT = (5, 20)
DOUBAN_BOOK_URL_PATTERN = re.compile(r".*/subject/(\d+)/?")
DOUBAN_BOOK_ID_PATTERN = re.compile(r"sid:\s*(?P<id>\d+),")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": DOUBAN_BASE,
}
IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": DOUBAN_BASE,
}

PROVIDER_NAME = "New Douban Books"
PROVIDER_ID = "new_douban"

log = logging.getLogger(__name__)
_thread_local = threading.local()


def _build_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_session():
    """Return one requests session per worker thread."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _build_session()
    return _thread_local.session


def _request(url, *, params=None, image=False, stream=False):
    headers = IMAGE_HEADERS if image else DEFAULT_HEADERS
    return _get_session().get(
        url,
        params=params,
        headers=headers,
        timeout=DOUBAN_REQUEST_TIMEOUT,
        stream=stream,
    )


def _is_allowed_cover_url(url):
    """Only allow public Douban image hosts through the cover proxy."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError):
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and (hostname == DOUBAN_COVER_DOMAIN or hostname.endswith("." + DOUBAN_COVER_DOMAIN))
    )


def _unwrap_cover_url(url):
    """Return the original image URL when passed one of our proxy URLs."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if query.get("cover"):
            return query["cover"][0]
    except (TypeError, ValueError):
        pass
    return url


class NewDouban(Metadata):
    __name__ = PROVIDER_NAME
    __id__ = PROVIDER_ID

    def __init__(self):
        self.searcher = DoubanBookSearcher()
        self.hack_helper_cover()
        super().__init__()

    def search(self, query: str, generic_cover: str = "", locale: str = "en"):
        if not self.active:
            return []
        return self.searcher.search_books(query, generic_cover)

    @staticmethod
    def hack_helper_cover():
        """Teach Calibre-Web's cover saver how to fetch proxied Douban images."""
        current_save_cover = helper.save_cover_from_url
        if getattr(current_save_cover, "_new_douban_wrapper", False):
            return

        def new_save_cover(url, book_path):
            cover_url = _unwrap_cover_url(url)
            if _is_allowed_cover_url(cover_url):
                try:
                    response = _request(cover_url, image=True)
                    if response.status_code == 200:
                        return helper.save_cover(response, book_path)
                    log.warning("Douban cover download returned HTTP %s", response.status_code)
                except requests.RequestException as error:
                    log.warning("Douban cover download failed: %s", error)
                return current_save_cover(cover_url, book_path)
            return current_save_cover(url, book_path)

        new_save_cover._new_douban_wrapper = True
        helper.save_cover_from_url = new_save_cover


@dataclasses.dataclass
class DoubanMetaRecord(MetaRecord):
    def __getattribute__(self, item):
        value = super().__getattribute__(item)
        if item != "cover" or not DOUBAN_PROXY_COVER or not _is_allowed_cover_url(value):
            return value

        host_url = DOUBAN_PROXY_COVER_HOST_URL
        if not host_url:
            try:
                host_url = request.host_url
            except (AttributeError, RuntimeError):
                return value
        if not host_url:
            return value

        return "{}{}{}".format(
            host_url.rstrip("/") + "/",
            DOUBAN_PROXY_COVER_PATH,
            urllib.parse.quote(value, safe=""),
        )


class DoubanBookSearcher:
    def __init__(self):
        self.book_loader = DoubanBookLoader()

    @staticmethod
    def calc_url(href):
        try:
            parsed = urllib.parse.urlparse(href)
            candidate = urllib.parse.parse_qs(parsed.query).get("url", [href])[0]
        except (TypeError, ValueError):
            return None
        if DOUBAN_BOOK_URL_PATTERN.fullmatch(candidate):
            return candidate
        return None

    def load_book_urls_from_html(self, query):
        response = _request(
            DOUBAN_SEARCH_URL,
            params={"cat": DOUBAN_BOOK_CAT, "q": query},
        )
        if response.status_code not in (200, 201):
            return []

        html = etree.HTML(response.content)
        if html is None:
            return []

        book_urls = []
        for link in html.xpath('//a[@class="nbg"]'):
            book_url = self.calc_url(link.attrib.get("href", ""))
            if not book_url:
                match = DOUBAN_BOOK_ID_PATTERN.search(link.attrib.get("onclick", ""))
                if match:
                    book_url = "{}subject/{}/".format(DOUBAN_BASE, match.group("id"))
            if book_url and book_url not in book_urls:
                book_urls.append(book_url)
            if len(book_urls) >= DOUBAN_CONCURRENCY_SIZE:
                break
        return book_urls

    # Kept for callers that used the helper exposed by earlier plugin releases.
    def load_book_urls_new(self, query):
        return self.load_book_urls_from_html(query)

    @staticmethod
    def load_book_urls_from_json(query):
        response = _request(
            DOUBAN_SEARCH_JSON_URL,
            params={"cat": DOUBAN_BOOK_CAT, "q": query},
        )
        if response.status_code not in (200, 201):
            return []
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return []

        book_urls = []
        for item in payload.get("items", []):
            match = DOUBAN_BOOK_ID_PATTERN.search(item if isinstance(item, str) else str(item))
            if match:
                url = "{}subject/{}/".format(DOUBAN_BASE, match.group("id"))
                if url not in book_urls:
                    book_urls.append(url)
            if len(book_urls) >= DOUBAN_CONCURRENCY_SIZE:
                break
        return book_urls

    def search_books(self, query, generic_cover=""):
        try:
            book_urls = self.load_book_urls_from_html(query)
        except requests.RequestException as error:
            log.warning("Douban HTML search failed: %s", error)
            book_urls = []

        if not book_urls:
            try:
                book_urls = self.load_book_urls_from_json(query)
            except requests.RequestException as error:
                log.warning("Douban JSON search failed: %s", error)
                book_urls = []

        books = []
        with ThreadPoolExecutor(
            max_workers=DOUBAN_CONCURRENCY_SIZE,
            thread_name_prefix="douban_async",
        ) as thread_pool:
            pending = [
                thread_pool.submit(self.book_loader.load_book, url, generic_cover)
                for url in book_urls
            ]
            for future in as_completed(pending):
                try:
                    book = future.result()
                except Exception as error:
                    log.warning("Douban book processing failed: %s", error)
                    continue
                if book is not None:
                    books.append(book)
        return books


class DoubanBookLoader:
    def __init__(self):
        self.book_parser = DoubanBookHtmlParser()

    @lru_cache(maxsize=DOUBAN_BOOK_CACHE_SIZE)
    def load_book(self, url, generic_cover=""):
        response = _request(url)
        if response.status_code not in (200, 201):
            return None
        return self.book_parser.parse_book(
            url,
            response.content.decode("utf8", "ignore"),
            generic_cover,
        )


class DoubanBookHtmlParser:
    def __init__(self):
        self.id_pattern = DOUBAN_BOOK_URL_PATTERN
        self.date_pattern = re.compile(r"^(\d{4})(?:[-/.年](\d{1,2}))?(?:[-/.月](\d{1,2}))?")
        self.tag_pattern = re.compile(r"criteria = '(.+?)'")

    def parse_book(self, url, book_content, generic_cover=""):
        book = DoubanMetaRecord(
            id="",
            title="",
            authors=[],
            publisher="",
            description="",
            url=url,
            source=MetaSourceInfo(
                id=PROVIDER_ID,
                description=PROVIDER_NAME,
                link=DOUBAN_BASE,
            ),
        )
        html = etree.HTML(book_content)
        if html is None:
            return None

        title_element = html.xpath("//span[@property='v:itemreviewed']")
        book.title = self.get_text(title_element)
        share_element = html.xpath("//a[@data-url]")
        if share_element:
            book.url = share_element[0].attrib.get("data-url", url)
        id_match = self.id_pattern.fullmatch(book.url)
        if id_match:
            book.id = id_match.group(1)
            book.identifiers["douban"] = book.id

        img_element = html.xpath("//a[@class='nbg']")
        if img_element:
            cover = img_element[0].attrib.get("href", "")
            if cover and not cover.endswith("update_image"):
                book.cover = cover
        if not _is_allowed_cover_url(book.cover):
            book.cover = generic_cover

        rating_element = html.xpath("//strong[@property='v:average']")
        book.rating = self.get_rating(rating_element)

        for element in html.xpath("//span[@class='pl']"):
            text = self.get_text(element)
            if text.startswith("作者") or text.startswith("译者"):
                book.authors.extend([
                    self.get_text(author_element)
                    for author_element in filter(self.author_filter, element.findall("..//a"))
                ])
            elif text.startswith("出版社"):
                book.publisher = self.get_tail(element)
            elif text.startswith("副标题"):
                subtitle = self.get_tail(element)
                if subtitle:
                    book.title = book.title + ":" + subtitle
            elif text.startswith("出版年"):
                book.publishedDate = self.get_publish_date(self.get_tail(element))
            elif text.startswith("丛书"):
                book.series = self.get_text(element.getnext())
            elif text.startswith("ISBN") or text.startswith("统一书号"):
                book.identifiers["isbn"] = self.get_tail(element)

        summary_element = html.xpath("//div[@id='link-report']//div[@class='intro']")
        if summary_element:
            book.description = etree.tostring(
                summary_element[-1], encoding="utf8"
            ).decode("utf8").strip()

        tag_elements = html.xpath("//a[contains(@class, 'tag')]")
        if tag_elements:
            book.tags = [self.get_text(tag_element) for tag_element in tag_elements]
        else:
            book.tags = self.get_tags(book_content)
        return book

    def get_tags(self, book_content):
        tag_match = self.tag_pattern.search(book_content)
        if not tag_match:
            return []
        return [
            tag.replace("7:", "", 1)
            for tag in tag_match.group(1).split("|")
            if tag.startswith("7:")
        ]

    def get_publish_date(self, date_str):
        if not date_str:
            return date_str
        date_match = self.date_pattern.match(date_str)
        if not date_match:
            return date_str
        year, month, day = date_match.groups()
        return "{}-{:02d}-{:02d}".format(year, int(month or 1), int(day or 1))

    def get_rating(self, rating_element):
        return float(self.get_text(rating_element, "0")) / 2

    @staticmethod
    def author_filter(a_element):
        a_href = a_element.attrib.get("href", "")
        return "/author" in a_href or "/search" in a_href

    @staticmethod
    def get_text(element, default_str=""):
        text = default_str
        if isinstance(element, list) and element and element[0].text:
            text = element[0].text.strip()
        elif isinstance(element, etree._Element) and element.text:
            text = element.text.strip()
        return text or default_str

    def get_tail(self, element, default_str=""):
        text = default_str
        if isinstance(element, etree._Element) and element.tail:
            text = element.tail.strip()
            if not text:
                text = self.get_text(element.getnext(), default_str)
        return text or default_str


@meta.route("/metadata/douban_cover", methods=["GET"])
def proxy_douban_cover():
    cover_url = request.args.get("cover", "")
    if not _is_allowed_cover_url(cover_url):
        return Response("Invalid cover URL", status=400)

    try:
        response = _request(cover_url, image=True, stream=True)
    except requests.RequestException as error:
        log.warning("Douban cover proxy failed: %s", error)
        return Response("Cover request failed", status=502)

    if response.status_code != 200:
        response.close()
        return Response("Cover request failed", status=502)

    def generate():
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            response.close()

    return Response(
        generate(),
        content_type=response.headers.get("Content-Type", "image/jpeg"),
    )
