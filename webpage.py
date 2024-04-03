#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import re
from time import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

from app import logger
from http_cache import http_get
from http_cache import is_response_too_large
from oa_local import find_normalized_license
from open_location import OpenLocation
from util import NoDoiException
from util import elapsed
from convert_http_to_https import fix_url_scheme
from util import get_link_target
from util import get_tree
from util import is_same_publisher

DEBUG_SCRAPING = os.getenv('DEBUG_SCRAPING', False)


# it matters this is just using the header, because we call it even if the content
# is too large.  if we start looking in content, need to break the pieces apart.
def is_pdf_from_header(response):
    looks_good = False
    for k, v in response.headers.items():
        if v:
            key = k.lower()
            val = v.lower()

            if key == "content-type" and "application/pdf" in val:
                looks_good = True

            if key == 'content-disposition' and "pdf" in val:
                looks_good = True
            try:
                if key == 'content-length' and int(val) < 128:
                    looks_good = False
                    break
            except ValueError:
                logger.error('got a nonnumeric content-length header: {}'.format(val))
                looks_good = False
                break
    return looks_good


def is_a_pdf_page(response, page_publisher):
    bad_header_publishers = (
        'Addleton Academic Publishers',
    )

    if is_pdf_from_header(response) and page_publisher not in bad_header_publishers:
        if DEBUG_SCRAPING:
            logger.info("http header says this is a PDF {}".format(
                response.request.url if "request" in response.__dict__ else response.url)
            )
        return True

    # everything below here needs to look at the content
    # so bail here if the page is too big
    if is_response_too_large(response):
        if DEBUG_SCRAPING:
            logger.info("response is too big for more checks in is_a_pdf_page")
        return False

    content = response.text_big()

    # PDFs start with this character
    if re.match("%PDF", content):
        # Encrypted PDF
        if re.search(r'/Encrypt \d+ \d+ [A-Za-z]+\s>>\sstartxref', content):
            return False
        return True

    if page_publisher:
        says_free_publisher_patterns = [
            ("Wiley-Blackwell", '<span class="freeAccess" title="You have free access to this content">'),
            ("Wiley-Blackwell", '<iframe id="pdfDocument"'),
            ("JSTOR", r'<li class="download-pdf-button">.*Download PDF.*</li>'),
            ("Institute of Electrical and Electronics Engineers (IEEE)", r'<frame src="http://ieeexplore.ieee.org/.*?pdf.*?</frameset>'),
            ("IOP Publishing", r'Full Refereed Journal Article')
        ]
        for (publisher, pattern) in says_free_publisher_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE | re.DOTALL)
            if is_same_publisher(page_publisher, publisher) and matches:
                return True
    return False


def is_a_word_doc_from_header(response):
    looks_good = False
    for k, v in response.headers.items():
        if v:
            key = k.lower()
            val = v.lower()

            if key == "content-type" and (
                    "application/msword" in val or
                    "application/doc" in val or
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in val
            ):
                looks_good = True

            try:
                if key == 'content-length' and int(val) < 512:
                    looks_good = False
                    break

            except ValueError:
                logger.error('got a nonnumeric content-length header: {}'.format(val))
                looks_good = False
                break
    return looks_good


def is_a_word_doc(response):
    if is_a_word_doc_from_header(response):
        if DEBUG_SCRAPING:
            logger.info("http header says this is a word doc {}".format(response.request.url if "request" in response.__dict__ else response.url))
        return True

    # everything below here needs to look at the content
    # so bail here if the page is too big
    if is_response_too_large(response):
        if DEBUG_SCRAPING:
            logger.info("response is too big for more checks in is_a_word_doc")
        return False

    content = response.content_big()

    # docx
    if content[-22:].startswith(b'PK') and (response.url.endswith('.docx') or b'word/document.xml' in content):
        return True

    # doc
    if content.startswith(b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'):
        return True

    # rtf, one repo for now
    if 'kuleuven.be' in response.url  and '.rtf' in response.url and content.startswith(b'{\rtf'):
        return True

    return False


class Webpage(object):
    def __init__(self, **kwargs):
        self.url = None
        self.scraped_pdf_url = None
        self.scraped_open_metadata_url = None
        self.scraped_license = None
        self.scraped_version = None
        self.error = ""
        self.related_pub_doi = None
        self.related_pub_publisher = None
        self.match_type = None
        self.session_id = None
        self.endpoint_id = None
        self.base_id = None
        self.base_doc = None
        self.resolved_url = None
        self.resolved_http_status_code = None
        self.issn_l = None
        self.page_text = None
        self.pdf_content = None
        self.r = None
        for (k, v) in kwargs.items():
            self.__setattr__(k, v)
        if not self.url:
            self.url = "http://doi.org/{}".format(self.doi)

    # from https://stackoverflow.com/a/865272/596939
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    @property
    def doi(self):
        return self.related_pub_doi

    # sometimes overriden, for publisherwebpage
    @property
    def ask_slowly(self):
        return False

    @property
    def publisher(self):
        return self.related_pub_publisher

    def is_same_publisher(self, publisher):
        return is_same_publisher(self.related_pub_publisher, publisher)

    @property
    def fulltext_url(self):
        if self.scraped_pdf_url:
            return self.scraped_pdf_url
        if self.scraped_open_metadata_url:
            return self.scraped_open_metadata_url
        if self.is_open:
            return self.url
        return None

    @property
    def has_fulltext_url(self):
        if self.scraped_pdf_url or self.scraped_open_metadata_url:
            return True
        return False

    @property
    def is_open(self):
        # just having the license isn't good enough
        if self.scraped_pdf_url or self.scraped_open_metadata_url:
            return True
        return False

    def mint_open_location(self):
        my_location = OpenLocation()
        my_location.pdf_url = self.scraped_pdf_url
        my_location.metadata_url = self.scraped_open_metadata_url
        my_location.license = self.scraped_license
        my_location.doi = self.related_pub_doi
        my_location.evidence = self.open_version_source_string
        my_location.match_type = self.match_type
        my_location.pmh_id = self.base_id
        my_location.endpoint_id = self.endpoint_id
        my_location.base_doc = self.base_doc
        my_location.error = ""
        if self.is_open and not my_location.best_url:
            my_location.metadata_url = self.url
        return my_location

    def set_r_for_pdf(self):
        self.r = None
        try:
            self.r = http_get(url=self.scraped_pdf_url, stream=True, publisher=self.publisher, session_id=self.session_id, ask_slowly=self.ask_slowly)

        except requests.exceptions.ConnectionError as e:
            self.error += "ERROR: connection error on {} in set_r_for_pdf: {}".format(self.scraped_pdf_url, str(e))
            logger.info(self.error)
        except requests.Timeout as e:
            self.error += "ERROR: timeout error on {} in set_r_for_pdf: {}".format(self.scraped_pdf_url, str(e))
            logger.info(self.error)
        except requests.exceptions.InvalidSchema as e:
            self.error += "ERROR: InvalidSchema error on {} in set_r_for_pdf: {}".format(self.scraped_pdf_url, str(e))
            logger.info(self.error)
        except requests.exceptions.RequestException as e:
            self.error += "ERROR: RequestException in set_r_for_pdf"
            logger.info(self.error)
        except requests.exceptions.ChunkedEncodingError as e:
            self.error += "ERROR: ChunkedEncodingError error on {} in set_r_for_pdf: {}".format(self.scraped_pdf_url, str(e))
            logger.info(self.error)
        except NoDoiException as e:
            self.error += "ERROR: NoDoiException error on {} in set_r_for_pdf: {}".format(self.scraped_pdf_url, str(e))
            logger.info(self.error)
        except Exception as e:
            self.error += "ERROR: Exception error in set_r_for_pdf"
            logger.exception(self.error)

    def is_a_pdf_page(self):
        return is_a_pdf_page(self.r, self.publisher)

    def gets_a_pdf(self, link, base_url):

        if is_purchase_link(link):
            return False

        absolute_url = get_link_target(link.href, base_url)
        if DEBUG_SCRAPING:
            logger.info("checking to see if {} is a pdf".format(absolute_url))

        start = time()
        try:
            self.r = http_get(absolute_url, stream=True, publisher=self.publisher, session_id=self.session_id, ask_slowly=self.ask_slowly)

            if self.r.status_code != 200:
                if self.r.status_code in [401]:
                    # is unauthorized, so not open
                    pass
                else:
                    self.error += "ERROR: status_code={} on {} in gets_a_pdf".format(self.r.status_code, absolute_url)
                return False

            if self.is_a_pdf_page():
                self.pdf_content = self.r.content_big()
                return self.r.url

        except requests.exceptions.ConnectionError as e:
            self.error += "ERROR: connection error in gets_a_pdf for {}: {}".format(absolute_url, str(e))
            logger.info(self.error)
        except requests.Timeout as e:
            self.error += "ERROR: timeout error in gets_a_pdf for {}: {}".format(absolute_url, str(e))
            logger.info(self.error)
        except requests.exceptions.InvalidSchema as e:
            self.error += "ERROR: InvalidSchema error in gets_a_pdf for {}: {}".format(absolute_url, str(e))
            logger.info(self.error)
        except requests.exceptions.RequestException as e:
            self.error += "ERROR: RequestException error in gets_a_pdf"
            logger.info(self.error)
        except requests.exceptions.ChunkedEncodingError as e:
            self.error += "ERROR: ChunkedEncodingError error in gets_a_pdf for {}: {}".format(absolute_url, str(e))
            logger.info(self.error)
        except NoDoiException as e:
            self.error += "ERROR: NoDoiException error in gets_a_pdf for {}: {}".format(absolute_url, str(e))
            logger.info(self.error)
        except Exception as e:
            self.error += "ERROR: Exception error in gets_a_pdf"
            logger.exception(self.error)

        if DEBUG_SCRAPING:
            logger.info("we've decided this ain't a PDF. took {} seconds [{}]".format(
                elapsed(start), absolute_url))
        return False

    def gets_a_word_doc(self, link, base_url):
        if is_purchase_link(link):
            return False

        absolute_url = get_link_target(link.href, base_url)
        if DEBUG_SCRAPING:
            logger.info("checking to see if {} is a word doc".format(absolute_url))

        try:
            r = http_get(absolute_url, stream=True, publisher=self.publisher, session_id=self.session_id, ask_slowly=self.ask_slowly)

            if r.status_code != 200:
                return False

            if is_a_word_doc(r):
                return True

        except Exception as e:
            logger.exception('error in gets_a_word_doc: {}'.format(e))

        return False

    def is_known_bad_link(self, link):
        if re.search(r'^https?://repositorio\.uchile\.cl/handle', self.url):
            # these are abstracts
            return re.search(r'item_\d+\.pdf', link.href or '')

        if re.search(r'^https?://dial\.uclouvain\.be', self.r.url):
            # disclaimer parameter is an unstable key
            return re.search(r'downloader\.php\?.*disclaimer=', link.href or '')

        if re.search(r'^https?://(?:www)?\.goodfellowpublishers\.com', self.r.url):
            return re.search(r'free_files/', link.href or '', re.IGNORECASE)

        if re.search(r'^https?://(?:www)?\.intellectbooks\.com', self.r.url):
            return re.search(r'_nfc', link.href or '', re.IGNORECASE)

        if re.search(r'^https?://philpapers.org/rec/FISBAI', self.r.url):
            return link.href and link.href.endswith('FISBAI.pdf')

        if re.search(r'^https?://eresearch\.qmu\.ac\.uk/', self.r.url):
            return link.href and 'appendix.pdf' in link.href

        bad_meta_pdf_links = [
            r'^https?://cora\.ucc\.ie/bitstream/',  # https://cora.ucc.ie/handle/10468/3838
            r'^https?://zefq-journal\.com/',  # https://zefq-journal.com/article/S1865-9217(09)00200-1/pdf
            r'^https?://www\.nowpublishers\.com/',  # https://www.nowpublishers.com/article/Details/ENT-062
        ]

        if link.anchor == '<meta citation_pdf_url>':
            for url_pattern in bad_meta_pdf_links:
                if re.search(url_pattern, link.href or ''):
                    return True

        bad_meta_pdf_sites = [
            # https://researchonline.federation.edu.au/vital/access/manager/Repository/vital:11142
            r'^https?://researchonline\.federation\.edu\.au/vital/access/manager/Repository/',
            r'^https?://www.dora.lib4ri.ch/[^/]*/islandora/object/',
            r'^https?://ifs\.org\.uk/publications/',  # https://ifs.org.uk/publications/14795
            r'^https?://ogma\.newcastle\.edu\.au',  # https://nova.newcastle.edu.au/vital/access/manager/Repository/uon:6800/ATTACHMENT01
            r'^https?://cjon\.ons\.org',  # https://cjon.ons.org/file/laursenaugust2020cjonpdf/download
            r'^https?://nowpublishers\.com', # https://nowpublishers.com/article/Details/ENT-085-2
            r'^https?://dspace\.library\.uu\.nl',  # a better link with no redirect is in the page body
        ]

        if link.anchor == '<meta citation_pdf_url>':
            for url_pattern in bad_meta_pdf_sites:
                if re.search(url_pattern, self.r.url or ''):
                    return True

        if link.href == 'https://dsq-sds.org/article/download/298/345':
            return True

        return False

    def filter_link(self, link):
        return None if not link or self.is_known_bad_link(link) else link

    def find_pdf_link(self, page, page_with_scripts=None):

        if DEBUG_SCRAPING:
            logger.info("in find_pdf_link in {}".format(self.url))

        # before looking in links, look in meta for the pdf link
        # = open journal http://onlinelibrary.wiley.com/doi/10.1111/j.1461-0248.2011.01645.x/abstract
        # = open journal http://doi.org/10.1002/meet.2011.14504801327
        # = open repo http://hdl.handle.net/10088/17542
        # = open http://handle.unsw.edu.au/1959.4/unsworks_38708 cc-by

        # logger.info(page)

        links = [get_pdf_in_meta(page)] + [get_pdf_from_javascript(page_with_scripts or page)] + get_useful_links(page)

        for link in [x for x in links if x is not None]:
            if DEBUG_SCRAPING:
                logger.info("trying {}, {} in find_pdf_link".format(link.href, link.anchor))

            if self.is_known_bad_link(link):
                continue

            # there are some links that are SURELY NOT the pdf for this article
            if has_bad_anchor_word(link.anchor):
                continue

            # there are some links that are SURELY NOT the pdf for this article
            if has_bad_href_word(link.href):
                continue

            # don't include links with newlines
            if link.href and "\n" in link.href and not any(s in link.href for s in [
                'securityanddefence.pl'
            ]):
                continue

            if link.href.startswith('#'):
                continue

            # download link ANCHOR text is something like "manuscript.pdf" or like "PDF (1 MB)"
            # = open repo http://hdl.handle.net/1893/372
            # = open repo https://research-repository.st-andrews.ac.uk/handle/10023/7421
            # = open repo http://dro.dur.ac.uk/1241/
            if link.anchor and "pdf" in link.anchor.lower():
                # handle https://utpjournals.press/doi/full/10.3138/tjt-2021-0016
                if (
                    self.publisher
                    and self.is_same_publisher("University of Toronto Press Inc. (UTPress)")
                    and "epdf" in link.href
                ):
                    continue
                else:
                    return link

            # button says download
            # = open repo https://works.bepress.com/ethan_white/45/
            # = open repo http://ro.uow.edu.au/aiimpapers/269/
            # = open repo http://eprints.whiterose.ac.uk/77866/
            if "download" in link.anchor or "télécharger" in link.anchor:
                if "citation" in link.anchor:
                    pass
                else:
                    return link

            # want it to match for this one https://doi.org/10.2298/SGS0603181L
            # but not this one: 10.1097/00003643-201406001-00238
            if (
                self.publisher
                and not self.is_same_publisher("Ovid Technologies (Wolters Kluwer Health)")
                and not self.is_same_publisher("University of Toronto Press Inc. (UTPress)")
            ):
                if link.anchor and "full text" in link.anchor.lower():
                    return link

                # "article text"
                if link.anchor and 'текст статьи' in link.anchor.lower():
                    return link

            # https://www.oclc.org/research/publications/2020/resource-discovery-twenty-first-century-library.html
            if (
                re.search(r'^https?://(www\.)?oclc\.org', self.url)
                and link.href and link.href.endswith('.pdf')
                and link.anchor and ('download' in link.anchor.lower() or 'read' in link.anchor.lower())
            ):
                return link

            # https://www.aida-itea.org/index.php/revista-itea/contenidos?idArt=911&lang=esp
            if "aida-itea.org" in self.resolved_url and "pdf" in link.href:
                return link

            # http://www.rudmet.ru/journal/2021/article/33922/?language=en
            if (
                re.search(r'^https?://(www\.)?rudmet\.ru/journal/', self.resolved_url)
                and link.href and re.search(r'^https?://(www\.)?rudmet\.net/media/articles/.*\.pdf$', link.href)
            ):
                return link

            # https://dspace.library.uu.nl/handle/1874/354530
            # https://dspace.library.uu.nl/handle/1874/383562
            if (
                re.search(r'^https?://dspace\.library\.uu\.nl/', self.resolved_url)
                and link.anchor and  'open access version via utrecht university repository' in link.anchor.lower()
            ):
                return link


            # download link is identified with an image
            for img in link.findall(".//img"):
                try:
                    if "pdf" in img.attrib["src"].lower() or "pdf" in img.attrib["class"].lower():
                        return link
                except KeyError:
                    pass

            try:
                if "pdf" in link.attrib["title"].lower():
                    return link
                if "download/pdf" in link.href:
                    return link
            except KeyError:
                pass

            anchor = link.anchor or ''
            href = link.href or ''
            version_labels = ['submitted version', 'accepted version', 'published version']

            if (
                any(re.match(r'^{}(?:\s+\([0-9.,gmkb ]+\))?$'.format(label), anchor.lower()) for label in version_labels)
                and (href.lower().endswith('.pdf') or '.pdf?' in href.lower())
            ):
                return link

        return None



    def __repr__(self):
        return "<{} ({}) {}>".format(self.__class__.__name__, self.url, self.is_open)


class PublisherWebpage(Webpage):
    open_version_source_string = "publisher landing page"

    @property
    def ask_slowly(self):
        return True

    @staticmethod
    def use_resolved_landing_url(resolved_url):
        resolved_hostname = urlparse(resolved_url).hostname
        return resolved_hostname and resolved_hostname.endswith('journals.lww.com')

    def is_known_bad_link(self, link):
        if super(PublisherWebpage, self).is_known_bad_link(link):
            return True

        if re.search(r'^https?://www.reabic.net/journals/bir/', self.r.url):
            # doi.org urls go to issue page with links for all articles, e.g. https://doi.org/10.3391/bir.2019.8.1.08
            return True

        if re.search(r'^https?://nnw.cz', self.r.url):
            # doi.org urls go to issue page with links for all articles, e.g. http://nnw.cz/obsahy15.html#25.033
            return True

        # https://www.journal-imab-bg.org/issues-2018/issue4/vol24issue4p2228-2231.html
        if re.search(r'^https?://www\.journal-imab-bg\.org/', self.r.url) and link.href and link.href.endswith('TitlePage.pdf'):
            return True

        if re.search(r'^https?://(www\.)?molbiolcell\.org/', self.r.url) and link.href and link.href.endswith('special_issues'):
            return True

        if re.search(r'^https?://(www\.)?englishjournal\.net/', self.r.url) and link.href and link.href.endswith('AuthorshipResponsibility.pdf'):
            return True

        return False

    def _trust_pdf_landing_pages(self):
        if is_same_publisher(self.publisher, 'Oxford University Press (OUP)'):
            return False

        return True

    def scrape_for_fulltext_link(self, find_pdf_link=True, pdf_hint=None):
        landing_url = self.url

        if DEBUG_SCRAPING:
            logger.info("checking to see if {} says it is open".format(landing_url))

        start = time()
        self.resolved_http_status_code = -1
        try:
            self.r = http_get(landing_url, stream=True, publisher=self.publisher, session_id=self.session_id, ask_slowly=self.ask_slowly)
            self.resolved_url = self.r.url
            self.resolved_http_status_code = self.r.status_code
            resolved_host = urlparse(self.resolved_url).hostname or ''

            metadata_url = self.resolved_url if self.use_resolved_landing_url(self.resolved_url) else landing_url

            if self.r.status_code != 200:
                if self.r.status_code in [401]:
                    # is unauthorized, so not open
                    pass
                else:
                    self.error += "ERROR: status_code={} on {} in scrape_for_fulltext_link, skipping.".format(self.r.status_code, self.r.url)
                logger.info("DIDN'T GET THE PAGE: {}".format(self.error))
                # logger.debug(self.r.request.headers)
                return

            # example 10.1007/978-3-642-01445-1
            if "crossref.org/_deleted-doi/" in self.resolved_url:
                logger.info("this is a deleted doi")
                return

            # if our landing_url redirects to a pdf, we're done.
            # = open repo http://hdl.handle.net/2060/20140010374
            if self.is_a_pdf_page():
                if self._trust_pdf_landing_pages():
                    self.pdf_content = self.r.content_big()
                    if DEBUG_SCRAPING:
                        logger.info("this is a PDF. success! [{}]".format(landing_url))
                    self.scraped_pdf_url = landing_url
                    self.open_version_source_string = "open (via free pdf)"
                elif DEBUG_SCRAPING:
                    logger.info("landing page is an untrustworthy PDF {}".format(landing_url))
                # don't bother looking for open access lingo because it is a PDF (or PDF wannabe)
                return

            else:
                if DEBUG_SCRAPING:
                    logger.info("landing page is not a PDF for {}.  continuing more checks".format(landing_url))

            # get the HTML tree
            page = self.r.text_small()

            if page and not re.search(r'^\s*<', page):
                # needs to look like x/html
                if DEBUG_SCRAPING:
                    logger.info("not html. skipping")
                return

            self.page_text = page

            if resolved_host.endswith('ssrn.com'):
                # articles found here are no good. site requires a login after a few articles.
                logger.info('not looking for full text at {}'.format(resolved_host))
                return

            # get IEEE PDF from script. we might need it later.
            ieee_pdf = resolved_host.endswith('ieeexplore.ieee.org') and re.search(r'"pdfPath":\s*"(/ielx?7/[\d/]*\.pdf)"', page)

            soup = None

            try:
                soup = BeautifulSoup(page, 'html.parser')
                [script.extract() for script in soup('script')]
                [div.extract() for div in soup.find_all("div", {'class': 'table-of-content'})]
                [div.extract() for div in soup.find_all("li", {'class': 'linked-article__item'})]

                if self.is_same_publisher('Wiley'):
                    [div.extract() for div in soup.find_all('div', {'class': 'hubpage-menu'})]

                if self.is_same_publisher('Oncology Nursing Society (ONS)'):
                    [div.extract() for div in soup.find_all('div', {'class': 'view-issue-articles'})]

                page = str(soup)
            except Exception as e:
                logger.error('error parsing html, skipped script removal: {}'.format(e))

            if soup is not None:
                ojs_meta = soup.find('meta', {'name': 'generator', 'content': re.compile(r'^Open Journal Systems')})
                if ojs_meta is not None:
                    main_article_elements = soup.select('div[role="main"] li a[id^="article-"]')
                    if len(main_article_elements) > 1:
                        logger.info('looks like a full issue index from OJS, skipping full text search')
                        return

            license_search_text = page_potential_license_text(page)

            # Look for a pdf link. If we find one, look for a license.

            pdf_download_link = self.find_pdf_link(page) if find_pdf_link else None

            # if we haven't found a pdf yet, try known patterns
            if pdf_download_link is None:
                if ieee_pdf:
                    pdf_download_link = DuckLink(ieee_pdf.group(1).replace('iel7', 'ielx7'), 'download')
                elif any(resolved_host.endswith(x) for x in ['osf.io', 'psyarxiv.com']):
                    pdf_download_link = DuckLink(get_link_target('download', self.resolved_url), 'download')

            # if we haven't found a pdf yet, try the hint
            if pdf_download_link is None and pdf_hint:
                logger.info('using hint {}'.format(pdf_hint))
                pdf_download_link = DuckLink(pdf_hint, 'xref pdf url')

            if pdf_download_link is not None:
                pdf_url = get_link_target(pdf_download_link.href, self.r.url)

                # https://recyt.fecyt.es/index.php/EPI/article/view/epi.2020.may.15
                if (re.match(r'https?://recyt\.fecyt\.es/index\.php/EPI/article/view/', pdf_url)):
                    pdf_url = pdf_url.replace('/article/view/', '/article/download/')
                    pdf_download_link.href = pdf_download_link.href.replace('/article/view/', '/article/download/')

                if (re.match(r'https?://(www.)?mitpressjournals\.org/doi/full/10\.+', pdf_url) or
                        re.match(r'https?://(www.)?journals\.uchicago\.edu/doi/full/10\.+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/full/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/full/', '/doi/pdf/')

                if re.match(r'https?://(www.)?ascopubs\.org/doi/full/10\.+', pdf_url):
                    pdf_url = pdf_url.replace('/doi/full/', '/doi/pdfdirect/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/full/', '/doi/pdfdirect/')

                if (re.match(r'https?://(www\.)?ahajournals\.org/doi/reader/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/reader/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/reader/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?journals.sagepub.com/doi/reader/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/reader/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/reader/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?tandfonline.com/doi/epdf/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/epdf/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/epdf/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?ajronline.org/doi/epdf/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/epdf/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/epdf/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?pubs.acs.org/doi/epdf/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/epdf/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/epdf/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?royalsocietypublishing.org/doi/epdf/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/doi/epdf/', '/doi/pdf/')
                    pdf_download_link.href = pdf_download_link.href.replace('/doi/epdf/', '/doi/pdf/')

                if (re.match(r'https?://(www\.)?onlinelibrary.wiley.com/doi/epdf/10\..+', pdf_url)):
                    pdf_url = pdf_url.replace('/epdf/', '/pdfdirect/')
                    pdf_download_link.href = pdf_download_link.href.replace('/epdf/', '/pdfdirect/')

                if self.gets_a_pdf(pdf_download_link, self.r.url):
                    self.scraped_pdf_url = pdf_url
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via free pdf)"

                    if 'pdfs.journals.lww.com' in pdf_url and 'token=' in pdf_url:
                        # works, but expires. take the pdf_url, leave the medatada_url
                        self.scraped_pdf_url = None

                    # set the license if we can find one
                    scraped_license = _trust_publisher_license(self.resolved_url) and find_normalized_license(license_search_text)
                    if scraped_license:
                        self.scraped_license = scraped_license
                elif pdf_url and pdf_url.startswith('https://www.sciencedirect.com/science/article/pii/'):
                    # try sciencedirect manuscript with cookies
                    pdf_url = pdf_url.replace('/article/pii/', '/article/am/pii/')
                    pdf_download_link.href = pdf_download_link.href.replace('/article/pii/', '/article/am/pii/')

                    am_pdf_response = http_get(
                        pdf_url, stream=True, publisher=self.publisher,
                        session_id=self.session_id, ask_slowly=self.ask_slowly, cookies=self.r.cookies
                    )

                    if is_a_pdf_page(am_pdf_response, self.publisher):
                        self.scraped_open_metadata_url = metadata_url
                        self.scraped_pdf_url = pdf_url
                        self.open_version_source_string = "open (author manuscript)"


            # Look for patterns that indicate availability but not necessarily openness and make this a bronze location.

            bronze_url_snippet_patterns = [
                ('sciencedirect.com/', '<div class="OpenAccessLabel">open archive</div>'),
                ('sciencedirect.com/', r'<span[^>]*class="[^"]*pdf-download-label[^"]*"[^>]*>Download PDF</span>'),
                ('sciencedirect.com/', r'<span class="primary-cta-button-text|link-button-text">View\s*<strong>PDF</strong></span>'),
                ('onlinelibrary.wiley.com', '<div[^>]*class="doi-access"[^>]*>Free Access</div>'),
                ('openedition.org', r'<span[^>]*id="img-freemium"[^>]*></span>'),
                ('openedition.org', r'<span[^>]*id="img-openaccess"[^>]*></span>'),
                # landing page html is invalid: <span class="accesstext"></span>Free</span>
                ('microbiologyresearch.org', r'<span class="accesstext">(?:</span>)?Free'),
                ('journals.lww.com', r'<li[^>]*id="[^"]*-article-indicators-free"[^>]*>'),
                ('ashpublications.org', r'<i[^>]*class="[^"]*icon-availability_free'),
                ('academic.oup.com', r'<i[^>]*class="[^"]*icon-availability_free'),
                ('publications.aap.org', r'<i[^>]*class="[^"]*icon-availability_free'),
                ('degruyter.com/', '<span>Free Access</span>'),
                ('degruyter.com/', 'data-accessrestricted="false"'),
                ('practicalactionpublishing.com', r'<img [^>]*class="open-access-icon"'),
                ("iucnredlist.org", r'<title>'),
            ]

            for (url_snippet, pattern) in bronze_url_snippet_patterns:
                if url_snippet in self.resolved_url.lower() and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via free article)"

            bronze_publisher_patterns = [
                ("New England Journal of Medicine (NEJM/MMS)", '<meta content="yes" name="evt-free"'),
                ("Massachusetts Medical Society", '<meta content="yes" name="evt-free"'),
                ("University of Chicago Press", r'<img[^>]*class="[^"]*accessIconLocation'),
                ("Elsevier BV", r'<span[^>]*class="[^"]*article-header__access[^"]*"[^>]*>Open Archive</span>'),
            ]

            for (publisher, pattern) in bronze_publisher_patterns:
                if self.is_same_publisher(publisher) and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via free article)"

            bronze_journal_patterns = [
                ('1352-2310', r'<span[^>]*>Download PDF</span>'),
            ]

            for (issn_l, pattern) in bronze_journal_patterns:
                if self.issn_l == issn_l and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via free article)"

            bronze_citation_pdf_patterns = [
                r'^https?://www\.sciencedirect\.com/science/article/pii/S[0-9X]+/pdf(?:ft)?\?md5=[0-9a-f]+.*[0-9x]+-main.pdf$'
            ]

            citation_pdf_link = get_pdf_in_meta(page)

            if citation_pdf_link and citation_pdf_link.href:
                for pattern in bronze_citation_pdf_patterns:
                    if re.findall(pattern, citation_pdf_link.href, re.IGNORECASE | re.DOTALL):
                        logger.info('found bronzish citation_pdf_url {}'.format(citation_pdf_link.href))
                        self.scraped_open_metadata_url = metadata_url
                        self.open_version_source_string = "open (via free article)"

            # Look for some license-like patterns that make this a hybrid location.

            hybrid_url_snippet_patterns = [
                ('projecteuclid.org/', '<strong>Full-text: Open access</strong>'),
                ('sciencedirect.com/', '<div class="OpenAccessLabel">open access</div>'),
                ('journals.ametsoc.org/', r'src="/templates/jsp/_style2/_ams/images/access_free\.gif"'),
                ('apsjournals.apsnet.org', r'src="/products/aps/releasedAssets/images/open-access-icon\.png"'),
                ('psychiatriapolska.pl', 'is an Open Access journal:'),
                ('journals.lww.com', '<span class="[^>]*ejp-indicator--free'),
                ('journals.lww.com', r'<img[^>]*src="[^"]*/icon-access-open\.gif"[^>]*>'),
                ('iospress.com', r'<img[^>]*src="[^"]*/img/openaccess_icon.png[^"]*"[^>]*>'),
                ('rti.org/', r'</svg>[^<]*Open Access[^<]*</span>'),
                ('cambridge.org/', r'<span[^>]*class="open-access"[^>]*>Open access</span>'),
            ]

            for (url_snippet, pattern) in hybrid_url_snippet_patterns:
                if url_snippet in self.resolved_url.lower() and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via page says Open Access)"
                    self.scraped_license = "unspecified-oa"


            backup_hybrid_url_snippet_patterns = [
                ('degruyter.com/', '<span>Open Access</span>'),
            ]

            # should probably defer to scraped license for all publishers, but don't want to rock the boat yet
            if not self.scraped_license:
                for (url_snippet, pattern) in backup_hybrid_url_snippet_patterns:
                    if url_snippet in self.resolved_url.lower() and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                        self.scraped_open_metadata_url = metadata_url
                        self.open_version_source_string = "open (via page says Open Access)"
                        self.scraped_license = "unspecified-oa"

            # try the license tab on T&F pages
            # https://www.tandfonline.com/doi/full/10.1080/03057240.2018.1471391
            # https://www.tandfonline.com/action/showCopyRight?doi=10.1080%2F03057240.2018.1471391
            if not self.scraped_license:
                if url_match := re.match(r'^https?://(?:www\.)?tandfonline\.com/doi/full/(10\..+)', self.resolved_url, re.IGNORECASE):
                    license_tab_url = 'https://www.tandfonline.com/action/showCopyRight?doi={doi}'.format(doi=url_match.group(1))
                    logger.info(f'looking for license tab {license_tab_url} on T&F landing page {self.resolved_url}')
                    license_tab_response = http_get(
                        license_tab_url, stream=True, publisher=self.publisher,
                        session_id=self.session_id, ask_slowly=self.ask_slowly, cookies=self.r.cookies
                    )

                    if license_tab_response.status_code == 200:
                        license_tab_text = license_tab_response.text_small()
                        if license_tab_license := find_normalized_license(page_potential_license_text(license_tab_text)):
                            self.scraped_license = license_tab_license
                            logger.info(f'found license {self.scraped_license} on license tab')

            hybrid_publisher_patterns = [
                ("Informa UK Limited", "/accessOA.png"),
                ("Oxford University Press (OUP)", "<i class='icon-availability_open'"),
                ("Institute of Electrical and Electronics Engineers (IEEE)", r'"isOpenAccess":true'),
                ("Institute of Electrical and Electronics Engineers (IEEE)", r'"openAccessFlag":"yes"'),
                ("Informa UK Limited", "/accessOA.png"),
                ("Royal Society of Chemistry (RSC)", "/open_access_blue.png"),
                ("Cambridge University Press (CUP)", '<span class="icon access open-access cursorDefault">'),
                ("Wiley", r'<div[^>]*class="doi-access"[^>]*>Open Access</div>'),
            ]

            for (publisher, pattern) in hybrid_publisher_patterns:
                if self.is_same_publisher(publisher) and re.findall(pattern, page, re.IGNORECASE | re.DOTALL):
                    self.scraped_open_metadata_url = metadata_url
                    self.open_version_source_string = "open (via page says Open Access)"
                    self.scraped_license = "unspecified-oa"

            # Look for more license-like patterns that make this a hybrid location.
            # Extract the specific license if present.

            license_patterns = [
                r"(creativecommons.org/licenses/[a-z\-]+)",
                "distributed under the terms (.*) which permits",
                "This is an open access article under the terms (.*) which permits",
                "This is an open-access article distributed under the terms (.*), where it is permissible",
                "This is an open access article published under (.*) which permits",
                '<div class="openAccess-articleHeaderContainer(.*?)</div>',
                r'this article is published under the creative commons (.*) licence',
                r'This work is licensed under a Creative Commons (.*), which permits ',
            ]

            if _trust_publisher_license(self.resolved_url):
                for pattern in license_patterns:
                    matches = re.findall(pattern, license_search_text, re.IGNORECASE)
                    if matches:
                        self.scraped_open_metadata_url = metadata_url
                        normalized_license = find_normalized_license(matches[0])
                        self.scraped_license = normalized_license or 'unspecified-oa'
                        if normalized_license:
                            self.open_version_source_string = 'open (via page says license)'
                        else:
                            self.open_version_source_string = 'open (via page says Open Access)'

            if self.is_open:
                if self.scraped_open_metadata_url:
                    self.scraped_open_metadata_url = fix_url_scheme(self.scraped_open_metadata_url)

                if self.scraped_pdf_url:
                    self.scraped_pdf_url = fix_url_scheme(self.scraped_pdf_url)

                if DEBUG_SCRAPING:
                    logger.info("we've decided this is open! took {} seconds [{}]".format(
                        elapsed(start), landing_url))
                return True
            else:
                if DEBUG_SCRAPING:
                    logger.info("we've decided this doesn't say open. took {} seconds [{}]".format(
                        elapsed(start), landing_url))
                return False
        except requests.exceptions.ConnectionError as e:
            self.error += "ERROR: connection error in scrape_for_fulltext_link on {}: {}".format(landing_url, str(e))
            logger.info(self.error)
            return False
        except requests.Timeout as e:
            self.error += "ERROR: timeout error in scrape_for_fulltext_link on {}: {}".format(landing_url, str(e))
            logger.info(self.error)
            return False
        except requests.exceptions.InvalidSchema as e:
            self.error += "ERROR: InvalidSchema error in scrape_for_fulltext_link on {}: {}".format(landing_url, str(e))
            logger.info(self.error)
            return False
        except requests.exceptions.RequestException as e:
            self.error += "ERROR: RequestException error in scrape_for_fulltext_link"
            logger.info(self.error)
            return False
        except requests.exceptions.ChunkedEncodingError as e:
            self.error += "ERROR: ChunkedEncodingError error in scrape_for_fulltext_link on {}: {}".format(landing_url, str(e))
            logger.info(self.error)
            return False
        except NoDoiException as e:
            self.error += "ERROR: NoDoiException error in scrape_for_fulltext_link on {}: {}".format(landing_url, str(e))
            logger.info(self.error)
            return False
        except Exception as e:
            self.error += "ERROR: Exception error in scrape_for_fulltext_link"
            logger.exception(self.error)
            return False


def _trust_repo_license(resolved_url):
    hostname = urlparse(resolved_url).hostname
    if not hostname:
        return False

    trusted_hosts = [
        'babel.hathitrust.org',
        'quod.lib.umich.edu',
        'taju.uniarts.fi',
    ]

    for host in trusted_hosts:
        if hostname.endswith(host):
            return True

    return False


def _try_pdf_link_as_doc(resolved_url):
    hostname = urlparse(resolved_url).hostname
    if not hostname:
        return False

    doc_hosts = [
        'paleorxiv.org',
        'osf.io',
    ]

    for host in doc_hosts:
        if hostname.endswith(host):
            return True

    return False


def _trust_publisher_license(resolved_url):
    hostname = urlparse(resolved_url).hostname
    if not hostname:
        return True

    untrusted_hosts = [
        'indianjournalofmarketing.com',
        'rnajournal.cshlp.org',
        'press.umich.edu',
        'genome.cshlp.org',
        'press.umich.edu',
        'medlit.ru',
        'journals.eco-vector.com',
        'alife-robotics.co.jp',
        'un-pub.eu',
        'zniso.fcgie.ru',
        'molbiolcell.org',
        'jcog.com.tr',
        'aimsciences.org',
        'soed.in',
        'berghahnjournals.com',
        'ojs.ual.es',
        'cjc-online.ca',
    ]

    for host in untrusted_hosts:
        if hostname.endswith(host):
            logger.info('not trusting license from {}'.format(host))
            return False

    if hostname.endswith('rupress.org'):
        # landing pages have license text like "available after 6 months under ..."
        # we don't need this for new articles because the licenses are in Crossref
        volume_no = re.findall(r'rupress\.org/jcb/[^/]+/(\d+)', resolved_url)
        try:
            if volume_no and int(volume_no[0]) < 217:
                # 217 is the first volume in 2018, before that we need the license text but the delay is now irrelevant
                return True
            else:
                # 2018 or later, ignore the license and get it from Crossref
                return False
        except ValueError:
            return False

    return True


# abstract.  inherited by PmhRepoWebpage
class RepoWebpage(Webpage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fulltext_bytes = None
        self.fulltext_type = None

    @property
    def open_version_source_string(self):
        return self.base_open_version_source_string

    def set_fulltext(self, r, file_type):
        if not is_response_too_large(r):
            self.fulltext_bytes = r.content_big()
            self.fulltext_type = file_type

    @staticmethod
    def use_resolved_landing_url(resolved_url):
        resolved_hostname = urlparse(resolved_url).hostname
        return resolved_hostname and resolved_hostname.endswith('adsabs.harvard.edu')

    def scrape_for_fulltext_link(self, find_pdf_link=True, pdf_hint=None):
        url = self.url

        dont_scrape_list = [
                "ncbi.nlm.nih.gov",
                "europepmc.org",
                "/europepmc/",
                "pubmed",
                "elar.rsvpu.ru",  #these ones based on complaint in email
                "elib.uraic.ru",
                "elar.usfeu.ru",
                "elar.urfu.ru",
                "elar.uspu.ru"]
        for url_fragment in dont_scrape_list:
            if url_fragment in url:
                logger.info("not scraping {} because is on our do not scrape list.".format(url))
                return

        try:
            self.r = http_get(url, stream=True, publisher=self.publisher, session_id=self.session_id, ask_slowly=self.ask_slowly)
            self.resolved_url = self.r.url
            self.resolved_http_status_code = self.r.status_code
            metadata_url = self.resolved_url if self.use_resolved_landing_url(self.resolved_url) else url

            if self.r.status_code != 200:
                if self.r.status_code in [401]:
                    # not authorized, so not open
                    pass
                else:
                    self.error += "ERROR: status_code={} on {} in scrape_for_fulltext_link".format(self.r.status_code, url)
                return

            # if our url redirects to a pdf, we're done.
            # = open repo http://hdl.handle.net/2060/20140010374
            if self.is_a_pdf_page():
                if accept_direct_pdf_links(self.resolved_url):
                    if DEBUG_SCRAPING:
                        logger.info("this is a PDF. success! [{}]".format(self.resolved_url))
                    self.scraped_pdf_url = url
                    self.set_fulltext(self.r, 'pdf')
                else:
                    if DEBUG_SCRAPING:
                        logger.info("ignoring direct pdf link".format(self.resolved_url))
                return

            else:
                if DEBUG_SCRAPING:
                    logger.info("is not a PDF for {}.  continuing more checks".format(url))

            if is_a_word_doc(self.r):
                if DEBUG_SCRAPING:
                    logger.info("this is a word doc. success! [{}]".format(url))
                self.scraped_open_metadata_url = metadata_url
                return

            # now before reading the content, bail it too large
            if is_response_too_large(self.r):
                logger.info("landing page is too large, skipping")
                return

            # get the HTML tree
            page = self.r.text_small()
            page_with_scripts = page

            if page and not re.search(r'^\s*<', page):
                # needs to look like x/html
                return

            self.page_text = page

            # remove script tags
            try:
                soup = BeautifulSoup(page, 'html.parser')
                [script.extract() for script in soup('script')]
                page = str(soup)
            except Exception as e:
                logger.error('error parsing html, skipped script removal: {}'.format(e))

            # set the license if we can find one
            scraped_license = find_normalized_license(page)
            if scraped_license:
                self.scraped_license = scraped_license

            # set the version if se can find one
            scraped_version = _find_version(self.resolved_url, page)
            if scraped_version:
                self.scraped_version = scraped_version

            pdf_download_link = None
            # special exception for citeseer because we want the pdf link where
            # the copy is on the third party repo, not the cached link, if we can get it
            if url and "citeseerx.ist.psu.edu/" in url:
                matches = re.findall('<h3>Download Links</h3>.*?href="(.*?)"', page, re.DOTALL)
                if matches:
                    pdf_download_link = DuckLink(matches[0], "download")

            # osf doesn't have their download link in their pages
            # so look at the page contents to see if it is osf-hosted
            # if so, compute the url.  example:  http://osf.io/tyhqm
            elif page and "osf-cookie" in page:
                pdf_download_link = DuckLink("{}/download".format(url), "download")
                pdf_download_link.href = re.sub('//download$', '/download', pdf_download_link.href)

            # otherwise look for it the normal way
            else:
                pdf_download_link = self.find_pdf_link(page, page_with_scripts=page_with_scripts)

            if pdf_download_link is None:
                if re.search(r'https?://cdm21054\.contentdm\.oclc\.org/digital/collection/IR/id/(\d+)', self.resolved_url):
                    pdf_download_link = DuckLink(
                        '/digital/api/collection/IR/id/{}/download'.format(
                            re.search(
                                r'https?://cdm21054\.contentdm\.oclc\.org/digital/collection/IR/id/(\d+)',
                                self.resolved_url
                            ).group(1)
                        ),
                        'download'
                    )

            if pdf_download_link is not None:
                if DEBUG_SCRAPING:
                    logger.info("found a PDF download link: {} {} [{}]".format(
                        pdf_download_link.href, pdf_download_link.anchor, url))

                pdf_url = get_link_target(pdf_download_link.href, self.r.url)
                # if they are linking to a PDF, we need to follow the link to make sure it's legit
                if DEBUG_SCRAPING:
                    logger.info("checking to see the PDF link actually gets a PDF [{}]".format(url))

                if (pdf_download_link.anchor == '<meta citation_pdf_url>' and
                    re.match(r'https?://(www\.)?osti\.gov/servlets/purl/[0-9]+', pdf_url)):
                        # try the pdf URL with cookies
                        osti_pdf_response = http_get(
                            pdf_url, stream=True, publisher=self.publisher,
                            session_id=self.session_id, ask_slowly=self.ask_slowly, cookies=self.r.cookies
                        )

                        if is_a_pdf_page(osti_pdf_response, self.publisher):
                            self.scraped_open_metadata_url = metadata_url
                            direct_pdf_url = osti_pdf_response.url

                            # make sure the resolved PDF URL works without cookies before saving it
                            direct_pdf_response = http_get(
                                direct_pdf_url, stream=True, publisher=self.publisher,
                                session_id=self.session_id, ask_slowly=self.ask_slowly
                            )

                            if is_a_pdf_page(direct_pdf_response, self.publisher):
                                self.scraped_pdf_url = osti_pdf_response.url
                                self.r = direct_pdf_response
                                self.set_fulltext(self.r, 'pdf')

                        return

                if self.gets_a_pdf(pdf_download_link, self.r.url):
                    self.scraped_open_metadata_url = metadata_url
                    if pdf_download_link.anchor and 'accepted version' in pdf_download_link.anchor.lower():
                        self.scraped_version = 'acceptedVersion'
                    if not _discard_pdf_url(pdf_url, self.resolved_url):
                        self.scraped_pdf_url = pdf_url
                        self.set_fulltext(self.r, 'pdf')
                    return


            # try this later because would rather get a pdf
            # if they are linking to a .docx or similar, this is open.
            doc_link = find_doc_download_link(page)
            if doc_link is None and _try_pdf_link_as_doc(self.resolved_url):
                doc_link = pdf_download_link

            if doc_link is not None:
                absolute_doc_url = get_link_target(doc_link.href, self.resolved_url)
                if DEBUG_SCRAPING:
                    logger.info("found a possible .doc download link [{}]".format(absolute_doc_url))
                if self.gets_a_word_doc(doc_link, self.r.url):
                    if DEBUG_SCRAPING:
                        logger.info("we've decided this is a word doc. [{}]".format(absolute_doc_url))
                    self.scraped_open_metadata_url = metadata_url
                    return
                else:
                    if DEBUG_SCRAPING:
                        logger.info("we've decided this ain't a word doc. [{}]".format(absolute_doc_url))

            bhl_link = find_bhl_view_link(self.resolved_url, page)
            if bhl_link is not None:
                logger.info('found a BHL document link: {}'.format(get_link_target(bhl_link.href, self.resolved_url)))
                self.scraped_open_metadata_url = metadata_url
                return

            if _trust_repo_license(self.resolved_url) and self.scraped_license:
                logger.info('trusting license {}'.format(self.scraped_license))
                self.scraped_open_metadata_url = metadata_url

        except requests.exceptions.ConnectionError as e:
            self.error += "ERROR: connection error on {} in scrape_for_fulltext_link: {}".format(url, str(e))
            logger.info(self.error)
            return
        except requests.Timeout as e:
            self.error += "ERROR: timeout error on {} in scrape_for_fulltext_link: {}".format(url, str(e))
            logger.info(self.error)
            return
        except requests.exceptions.InvalidSchema as e:
            self.error += "ERROR: InvalidSchema error on {} in scrape_for_fulltext_link: {}".format(url, str(e))
            logger.info(self.error)
            return
        except requests.exceptions.RequestException as e:
            self.error += "ERROR: RequestException in scrape_for_fulltext_link"
            logger.info(self.error)
            return
        except requests.exceptions.ChunkedEncodingError as e:
            self.error += "ERROR: ChunkedEncodingError error on {} in scrape_for_fulltext_link: {}".format(url, str(e))
            logger.info(self.error)
            return
        except NoDoiException as e:
            self.error += "ERROR: NoDoiException error on {} in scrape_for_fulltext_link: {}".format(url, str(e))
            logger.info(self.error)
            return
        except Exception as e:
            self.error += "ERROR: Exception error on in scrape_for_fulltext_link"
            logger.exception(self.error)
            return

        if DEBUG_SCRAPING:
            logger.info("found no PDF download link.  end of the line. [{}]".format(url))

        return self


def _find_version(url, page):
    hostname = urlparse(url).hostname

    if hostname and hostname.endswith('serval.unil.ch'):
        if "Version: Final published version" in page:
            return 'publishedVersion'
        if "Version: Author's accepted manuscript" in page:
            return 'acceptedVersion'

    if hostname and hostname.endswith('repository.lboro.ac.uk'):
        if "AM (Accepted Manuscript)" in page:
            return 'acceptedVersion'

    return None


def accept_direct_pdf_links(url):
    if re.match(r'^https?://pure\.mpg\.de', url):
        # direct pdf lnks to supplementary materials
        return False

    if re.match(r'^https?://authors\.library\.caltech\.edu', url):
        # direct pdf lnks to supplementary materials
        return False

    return True


class PmhRepoWebpage(RepoWebpage):
    @property
    def base_open_version_source_string(self):
        if self.match_type:
            return "oa repository (via OAI-PMH {} match)".format(self.match_type)
        return "oa repository (via OAI-PMH)"


def find_doc_download_link(page):
    for link in get_useful_links(page):
        # there are some links that are FOR SURE not the download for this article
        if has_bad_href_word(link.href):
            continue

        if has_bad_anchor_word(link.anchor):
            continue

        # = open repo https://lirias.kuleuven.be/handle/123456789/372010
        if ".doc" in link.href or ".doc" in link.anchor:
            if DEBUG_SCRAPING:
                logger.info("link details: {} {}".format(link.href, link.anchor))
            return link

    return None


def find_bhl_view_link(url, page_content):
    hostname = urlparse(url).hostname
    if not (hostname and hostname.endswith('biodiversitylibrary.org')):
        return None

    view_links = [link for link in get_useful_links(page_content) if link.anchor == 'view article']
    return view_links[0] if view_links else None


class DuckLink(object):
    def __init__(self, href, anchor):
        self.href = href
        self.anchor = anchor


def get_useful_links(page):
    links = []

    tree = get_tree(page)
    if tree is None:
        return []

    # remove related content sections

    bad_section_finders = [
        # references and related content sections

        "//div[@class=\'relatedItem\']",  #http://www.tandfonline.com/doi/abs/10.4161/auto.19496
        "//ol[@class=\'links-for-figure\']",  #http://www.tandfonline.com/doi/abs/10.4161/auto.19496
        "//div[@class=\'citedBySection\']",  #10.3171/jns.1966.25.4.0458
        "//div[@class=\'references\']",  #https://www.emeraldinsight.com/doi/full/10.1108/IJCCSM-04-2017-0089
        "//div[@class=\'moduletable\']",  # http://vestnik.mrsu.ru/index.php/en/articles2-en/80-19-1/671-10-15507-0236-2910-029-201901-1
        "//div[contains(@class, 'ref-list')]", #https://www.jpmph.org/journal/view.php?doi=10.3961/jpmph.16.069
        "//div[contains(@class, 'references')]", #https://venue.ep.liu.se/article/view/1498
        "//div[@id=\'supplementary-material\']", #https://www.jpmph.org/journal/view.php?doi=10.3961/jpmph.16.069
        "//div[@id=\'toc\']",  # https://www.elgaronline.com/view/edcoll/9781781004326/9781781004326.xml
        "//div[contains(@class, 'cta-guide-authors')]",  # https://www.journals.elsevier.com/physics-of-the-dark-universe/
        "//div[contains(@class, 'footer-publication')]",  # https://www.journals.elsevier.com/physics-of-the-dark-universe/
        "//d-appendix",  # https://distill.pub/2017/aia/
        "//dt-appendix",  # https://distill.pub/2016/handwriting/
        "//div[starts-with(@id, 'dt-cite')]",  # https://distill.pub/2017/momentum/
        "//ol[contains(@class, 'ref-item')]",  # http://www.cjcrcn.org/article/html_9778.html
        "//div[contains(@class, 'NLM_back')]",      # https://pubs.acs.org/doi/10.1021/acs.est.7b05624
        "//div[contains(@class, 'NLM_citation')]",  # https://pubs.acs.org/doi/10.1021/acs.est.7b05624
        "//div[@id=\'relatedcontent\']",            # https://pubs.acs.org/doi/10.1021/acs.est.7b05624
        "//div[@id=\'author-infos\']",  # https://www.tandfonline.com/doi/full/10.1080/01639374.2019.1670767
        "//ul[@id=\'book-metrics\']",   # https://link.springer.com/book/10.1007%2F978-3-319-63811-9
        "//section[@id=\'article_references\']",   # https://www.nejm.org/doi/10.1056/NEJMms1702111
        "//section[@id=\'SupplementaryMaterial\']",   # https://link.springer.com/article/10.1057%2Fs41267-018-0191-3
        "//div[@id=\'attach_additional_files\']",   # https://digitalcommons.georgiasouthern.edu/ij-sotl/vol5/iss2/14/
        "//span[contains(@class, 'fa-lock')]",  # https://www.dora.lib4ri.ch/eawag/islandora/object/eawag%3A15303
        "//ul[@id=\'reflist\']",  # https://elibrary.steiner-verlag.de/article/10.25162/sprib-2019-0002
        "//div[@class=\'listbibl\']",  # http://sk.sagepub.com/reference/the-sage-handbook-of-television-studies
        "//div[contains(@class, 'summation-section')]",  # https://www.tandfonline.com/eprint/EHX2T4QAGTIYVPK7MJBF/full?target=10.1080/20507828.2019.1614768
        "//ul[contains(@class, 'references')]",  # https://www.tandfonline.com/eprint/EHX2T4QAGTIYVPK7MJBF/full?target=10.1080/20507828.2019.1614768
        "//p[text()='References']/following-sibling::p", # http://researcherslinks.com/current-issues/Effect-of-Different-Temperatures-on-Colony/20/1/2208/html
        "//span[contains(@class, 'ref-lnk')]",  # https://www.tandfonline.com/doi/full/10.1080/19386389.2017.1285143
        "//div[@id=\'referenceContainer\']",  # https://www.jbe-platform.com/content/journals/10.1075/ld.00050.kra
        "//div[contains(@class, 'table-of-content')]",  # https://onlinelibrary.wiley.com/doi/book/10.1002/9781118897126
        "//img[contains(@src, 'supplementary_material')]/following-sibling::p", # https://pure.mpg.de/pubman/faces/ViewItemOverviewPage.jsp?itemId=item_2171702
        "//span[text()[contains(., 'Supplemental Material')]]/parent::td/parent::tr",  # https://authors.library.caltech.edu/56142/
        "//div[@id=\'utpPrimaryNav\']",  # https://utpjournals.press/doi/10.3138/jsp.51.4.10
        "//p[@class=\'bibentry\']",  # http://research.ucc.ie/scenario/2019/01/Voelker/12/de
        "//a[contains(@class, 'cover-out')]",  # https://doi.org/10.5152/dir.2019.18142
        "//div[@class=\'footnotes\']",  # https://mhealth.jmir.org/2020/4/e19359/
        "//h2[text()='References']/following-sibling::ul",  # http://hdl.handle.net/2027/spo.17063888.0037.114
        "//section[@id=\'article-references\']",  # https://journals.lww.com/academicmedicine/Fulltext/2015/05000/Implicit_Bias_Against_Sexual_Minorities_in.8.aspx
        "//div[@class=\'refs\']",  # https://articles.math.cas.cz/10.21136/AM.2020.0344-19
        "//div[@class=\'citation-content\']",  # https://cdnsciencepub.com/doi/10.1139/cjz-2019-0247
        "//li[@class=\'refbiblio\']",  # https://www.erudit.org/fr/revues/documentation/2021-v67-n1-documentation05867/1075634ar/
        "//div[@class=\'Citation\']", # https://mijn.bsl.nl/seksualiteit-kinderwens-vruchtbaarheidsproblemen-en-vruchtbaarhe/16090564
        "//section[@id=\'ej-article-sam-container\']", # https://journals.lww.com/epidem/Fulltext/2014/09000/Elemental_Composition_of_Particulate_Matter_and.5.aspx
        "//h4[text()='References']/following-sibling::p",  # https://editions.lib.umn.edu/openrivers/article/mapping-potawatomi-presences/
        "//li[contains(@class, 'article-references')]",  # https://www.nejm.org/doi/10.1056/NEJMc2032052
        "//section[@id=\'supplementary-materials']", # https://www.science.org/doi/pdf/10.1126/science.aan5893
        "//td[text()='References']/following-sibling::td", # http://www.rudmet.ru/journal/2021/article/33922/?language=en
        "//article[@id=\'ej-article-view\']//div[contains(@class, 'ejp-fulltext-content')]//p[contains(@id, 'JCL-P')]",  # https://journals.lww.com/oncology-times/Fulltext/2020/11200/UpToDate.4.aspx
        "//span[contains(@class, 'ref-list')]//span[contains(@class, 'reference')]", #  https://www.degruyter.com/document/doi/10.1515/ijamh-2020-0111/html
        "//div[contains(@class, 'ncbiinpagenav')]",  # https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6657953/
        "//h4[text()[contains(., 'Multimedia Appendix')]]/following-sibling::a",  # https://www.researchprotocols.org/2019/1/e11540/
        "//section[contains(@class, 'references')]",  # http://ojs.ual.es/ojs/index.php/eea/article/view/5974
        "//h3[text()='Acknowledgements']/following-sibling::p",  # https://www.tandfonline.com/doi/full/10.1080/02635143.2016.1248928
        "//div[@id=\'references-list\']",  # https://www.cambridge.org/core/books/abs/juries-lay-judges-and-mixed-courts/worldwide-perspective-on-lay-participation/E0CA7057A55D03C4500371752E352571
        "//h2[text()='Notes']/following-sibling::ol//p[@class=\'alinea\']", # https://www.erudit.org/fr/revues/im/2015-n26-im02640/1037312ar/
        "//h2[text()='Policies and information']/following-sibling::ul",  # https://www.emerald.com/insight/content/doi/10.1108/RSR-06-2021-0025/full/html

        # can't tell what chapter/section goes with what doi
        "//div[@id=\'booktoc\']",  # https://link.springer.com/book/10.1007%2F978-3-319-63811-9
        "//div[@id=\'tocWrapper\']",  # https://www.elgaronline.com/view/edcoll/9781786431417/9781786431417.xml
        "//tr[@class=\'bookTocEntryRow\']",  # https://www.degruyter.com/document/doi/10.3138/9781487514976/html
    ]

    for section_finder in bad_section_finders:
        for bad_section in tree.xpath(section_finder):
            bad_section.clear()

    # now get the links
    link_elements = tree.xpath("//a")

    for link in link_elements:
        link_text = link.text_content().strip().lower()
        if link_text:
            link.anchor = link_text
            if "href" in link.attrib:
                link.href = link.attrib["href"]
        elif "data-tooltip" in link.attrib and 'download pdf' in link.attrib['data-tooltip'].lower():
            link.anchor = link.attrib['data-tooltip']
            if 'href' in link.attrib:
                link.href = link.attrib['href']
        elif 'title' in link.attrib and 'download fulltext' in link.attrib['title'].lower():
            link.anchor = 'title: {}'.format(link.attrib['title'])
            if 'href' in link.attrib:
                link.href = link.attrib['href']
        elif 'href' in link.attrib and '?create_pdf_query' in link.attrib['href'].lower():
            link.anchor = 'pdf_generator'
            link.href = link.attrib['href']
        else:
            # also a useful link if it has a solo image in it, and that image includes "pdf" in its filename
            link_content_elements = [l for l in link]
            if len(link_content_elements) == 1:
                link_insides = link_content_elements[0]
                if link_insides.tag == "img":
                    if "src" in link_insides.attrib and "pdf" in link_insides.attrib["src"]:
                        link.anchor = "image: {}".format(link_insides.attrib["src"])
                        if "href" in link.attrib:
                            link.href = link.attrib["href"]

        if hasattr(link, "anchor") and hasattr(link, "href"):
            links.append(link)

    return links


def page_potential_license_text(page):
    tree = get_tree(page)

    if tree is None:
        return page

    section_removed = False

    bad_section_finders = [
        "//div[contains(@class, 'view-pnas-featured')]",  # https://www.pnas.org/content/114/38/10035
        "//meta[contains(@name, 'citation_reference')]",  # https://www.thieme-connect.de/products/ebooks/lookinside/10.1055/sos-SD-226-00098
    ]

    for section_finder in bad_section_finders:
        for bad_section in tree.xpath(section_finder):
            bad_section.clear()
            section_removed = True

    if not section_removed:
        return page

    try:
        return etree.tostring(tree, encoding=str)
    except Exception:
        return page


def is_purchase_link(link):
    # = closed journal http://www.sciencedirect.com/science/article/pii/S0147651300920050
    if "purchase" in link.anchor:
        logger.info("found a purchase link! {} {}".format(link.anchor, link.href))
        return True
    return False


def has_bad_href_word(href):
    href_blacklist = [
        # = closed 10.1021/acs.jafc.6b02480
        # editorial and advisory board
        "/eab/",

        # = closed 10.1021/acs.jafc.6b02480
        "/suppl_file/",

        # https://lirias.kuleuven.be/handle/123456789/372010
        "supplementary+file",

        # http://www.jstor.org/action/showSubscriptions
        "showsubscriptions",

        # 10.7763/ijiet.2014.v4.396
        "/faq",

        # 10.1515/fabl.1988.29.1.21
        "{{",

        # 10.2174/1389450116666150126111055
        "cdt-flyer",

        # 10.1111/fpa.12048
        "figures",

        # https://www.crossref.org/iPage?doi=10.3138%2Fecf.22.1.1
        "price-lists",

        # https://aaltodoc.aalto.fi/handle/123456789/30772
        "aaltodoc_pdf_a.pdf",

        # prescribing information, see http://www.nejm.org/doi/ref/10.1056/NEJMoa1509388#t=references
        "janssenmd.com",

        # prescribing information, see http://www.nejm.org/doi/ref/10.1056/NEJMoa1509388#t=references
        "community-register",

        # prescribing information, see http://www.nejm.org/doi/ref/10.1056/NEJMoa1509388#t=references
        "quickreference",

        # 10.4158/ep.14.4.458
        "libraryrequestform",

        # http://www.nature.com/nutd/journal/v6/n7/full/nutd201620a.html
        "iporeport",

        #https://ora.ox.ac.uk/objects/uuid:06829078-f55c-4b8e-8a34-f60489041e2a
        "no_local_copy",

        ".zip",

        # https://zenodo.org/record/1238858
        ".gz",

        # https://zenodo.org/record/1238858
        ".tar.",

        # http://www.bioone.org/doi/full/10.1642/AUK-18-8.1
        "/doi/full/10.1642",

        # dating site :(  10.1137/S0036142902418680 http://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.144.7627
        "hyke.org",

        # is a citation http://orbit.dtu.dk/en/publications/autonomous-multisensor-microsystem-for-measurement-of-ocean-water-salinity(1dea807b-c309-40fd-a623-b6c28999f74f).html
        "&rendering=",

        ".fmatter",

        "/samples/",

        # http://ira.lib.polyu.edu.hk/handle/10397/78907
        "letter_to_publisher",

        # https://www.sciencedirect.com/science/article/abs/pii/S1428226796700911?via%3Dihub
        'first-page',

        # https://www.mitpressjournals.org/doi/abs/10.1162/evco_a_00219
        'lib_rec_form',

        # http://www.eurekaselect.com/107875/chapter/climate-change-and-snow-cover-in-the-european-alp
        'ebook-flyer',

        # http://digital.csic.es/handle/10261/134122
        'accesoRestringido',

        # https://www.springer.com/statistics/journal/11222
        '/productFlyer/',

        # https://touroscholar.touro.edu/nymc_fac_pubs/622/
        '/author_agreement',

        # http://orca.cf.ac.uk/115888/
        'supinfo.pdf',

        # http://orca.cf.ac.uk/619/
        '/Appendix',

        # https://digitalcommons.fairfield.edu/business-facultypubs/31/
        'content_policy.pdf',

        # http://cds.cern.ch/record/1338672
        'BookTOC.pdf',
        'BookBackMatter.pdf',

        # https://www.goodfellowpublishers.com/academic-publishing.php?content=doi&doi=10.23912/9781911396512-3599
        'publishers-catalogue',

        # https://orbi.uliege.be/handle/2268/212705
        "_toc_",

        # https://pubs.usgs.gov/of/2004/1004/
        "adobe.com/products/acrobat",

        # https://physics.aps.org/articles/v13/31
        "featured-article-pdf",

        # http://www.jstor.org.libezproxy.open.ac.uk/stable/1446650
        "modern-slavery-act-statement.pdf",

        # https://pearl.plymouth.ac.uk/handle/10026.1/15597
        "Deposit_Agreement",

        # https://www.e-elgar.com/shop/gbp/the-elgar-companion-to-social-economics-second-edition-9781783478538.html
        '/product_flyer/',

        # https://journals.lww.com/jbjsjournal/FullText/2020/05200/Better_Late_Than_Never,_but_Is_Early_Best__.15.aspx
        'links.lww.com/JBJS/F791',

        # https://ctr.utpjournals.press/doi/10.3138/ctr.171.005
        'ctr_media_kit',
        'ctr_advertising_rates',

        # https://www.taylorfrancis.com/books/9780429465307
        'format=googlePreviewPdf',
        'type=googlepdf',

        # https://doaj.org/article/09fd431c6c99432490d9c4dfbfb2be98
        'guide_authors',

        # http://cds.cern.ch/record/898845/files/
        '_TOC.pdf',
        '_BookBackMatter.pdf',
        '_BookTOC.pdf',

        # https://www.econometricsociety.org/publications/econometrica/2019/05/01/distributional-framework-matched-employer-employee-data
        '-supplement.pdf',

        # https://www.thebhs.org/publications/the-herpetological-journal/volume-29-number-3-july-2019/1935-06-observations-of-threatened-asian-box-turtles-i-cuora-i-spp-on-trade-in-vietnam
        'ethicspolicy.pdf',

        # https://journals.lww.com/annalsofsurgery/Abstract/9000/Frailty_in_Older_Patients_Undergoing_Emergency.95070.aspx
        'coi_disclosure.pdf',

        # https://doi.org/10.1504/ijbge.2020.10028180
        '_leaflet.pdf',

        # https://search.mandumah.com/Record/1037229
        'User-manual.pdf',

        # https://dspace.stir.ac.uk/handle/1893/27593
        'table_final.pdf',

        # https://www.jmcp.org/doi/full/10.18553/jmcp.2019.25.7.817
        '/doi/full/10.18553/jmcp.',

        # http://repository.bilkent.edu.tr/handle/11693/75891
        'Bilkent-research-paper.pdf',

        # http://repositorio.conicyt.cl/handle/10533/172208
        'guia_busquedas_avanzadas.pdf',

        # https://journals.physiology.org/doi/abs/10.1152/ajplegacy.1910.26.6.413
        'PDFs/2017-Legacy-1516816496183.pdf',

        # https://opendocs.ids.ac.uk/opendocs/handle/20.500.12413/14067
        'TermsOfUse.pdf',

        # https://www.techscience.com/cmc/v70n3/44999
        'javascript:void',

        # https://www.nowpublishers.com/article/Details/FIN-015
        '/DownloadSummary/',

        # https://repositorio.unesp.br/handle/11449/161850
        'WOS000382116900027.pdf',
    ]

    href_whitelist = [
        # https://zenodo.org/record/3831263
        '190317_MainText_Figures_JNNP.pdf',
        # https://archive.nyu.edu/handle/2451/34777?mode=full
        'Using%20Google%20Forms%20to%20Track%20Library%20Space%20Usage%20w%20figures.pdf',
    ]

    for good_word in href_whitelist:
        if good_word.lower() in href.lower():
            return False

    for bad_word in href_blacklist:
        if bad_word.lower() in href.lower():
            return True

    bad_patterns = [
        r'jmir_v[a-z0-9]+_app\d+\.pdf',  # https://www.jmir.org/2019/9/e15011
    ]

    for bad_pattern in bad_patterns:
        if re.findall(bad_pattern, href, re.IGNORECASE):
            return True

    return False


def has_bad_anchor_word(anchor_text):
    anchor_blacklist = [
        # = closed repo https://works.bepress.com/ethan_white/27/
        "user",
        "guide",

        # = closed 10.1038/ncb3399
        "checklist",

        # wrong link
        "abstracts",

        # http://orbit.dtu.dk/en/publications/autonomous-multisensor-microsystem-for-measurement-of-ocean-water-salinity(1dea807b-c309-40fd-a623-b6c28999f74f).html
        "downloaded publications",

        # https://hal.archives-ouvertes.fr/hal-00085700
        "metadata from the pdf file",
        "récupérer les métadonnées à partir d'un fichier pdf",

        # = closed http://europepmc.org/abstract/med/18998885
        "bulk downloads",

        # http://www.utpjournals.press/doi/pdf/10.3138/utq.35.1.47
        "license agreement",

        # = closed 10.1021/acs.jafc.6b02480
        "masthead",

        # closed http://eprints.soton.ac.uk/342694/
        "download statistics",

        # no examples for these yet
        "supplement",
        "figure",
        "faq",

        # https://www.biodiversitylibrary.org/bibliography/829
        "download MODS",
        "BibTeX citations",
        "RIS citations",

        'ACS ActiveView PDF',

        # https://doi.org/10.11607/jomi.4336
        'Submission Form',

        # https://doi.org/10.1117/3.651915
        'Sample Pages',

        # https://babel.hathitrust.org/cgi/pt?id=uc1.e0000431916&view=1up&seq=24
        'Download this page',
        'Download left page',
        'Download right page',

        # https://touroscholar.touro.edu/nymc_fac_pubs/622/
        'author agreement',

        # https://www.longwoods.com/content/25849
        'map to our office',

        # https://www.e-elgar.com/shop/the-art-of-mooting
        'download flyer',

        # https://www.nowpublishers.com/article/Details/ENT-062
        'download extract',

        # https://utpjournals.press/doi/full/10.3138/jsp.48.3.137
        'Call for Papers',

        # https://brill.com/view/title/14711
        'View PDF Flyer',

        # https://doi.org/10.17582/journal.pjz/20190204150214
        'Full Text HTML',

        # https://openresearch-repository.anu.edu.au/password-login
        'Submitting an item to the Open Research repository',

        # https://www.wageningenacademic.com/doi/10.3920/BM2020.0057
        'Download our catalogue',

        # https://onlinelibrary.wiley.com/toc/15213994/1877/89/22
        'Reprint Order Form',
        'Cost Confirmation and Order Form',
    ]
    for bad_word in anchor_blacklist:
        if bad_word.lower() in anchor_text.lower():
            return True

    return False


def get_pdf_in_meta(page):
    if "citation_pdf_url" in page:
        if DEBUG_SCRAPING:
            logger.info("citation_pdf_url in page")

        tree = get_tree(page)
        if tree is not None:
            metas = tree.xpath("//meta")
            for meta in metas:
                meta_name = meta.attrib.get('name', None)
                meta_property = meta.attrib.get('property', None)

                if meta_name == "citation_pdf_url" or meta_property == "citation_pdf_url":
                    if "content" in meta.attrib:
                        link = DuckLink(href=meta.attrib["content"], anchor="<meta citation_pdf_url>")
                        return _transform_meta_pdf(link, page)
        else:
            # backup if tree fails
            regex = r'<meta name="citation_pdf_url" content="(.*?)">'
            matches = re.findall(regex, page)
            if matches:
                link = DuckLink(href=matches[0], anchor="<meta citation_pdf_url>")
                return _transform_meta_pdf(link, page)
    return None


def _transform_meta_pdf(link, page):
    if link and link.href:
        link.href = re.sub('(https?://[\w\.]*onlinelibrary.wiley.com/doi/)pdf(/.+)', r'\1pdfdirect\2', link.href)
        link.href = re.sub('(^https?://drops\.dagstuhl\.de/.*\.pdf)/$', r'\1', link.href)
        # https://repository.ubn.ru.nl/bitstream/2066/47467/1/47467.pdf ->
        # https://repository.ubn.ru.nl/bitstream/handle/2066/47467/1/47467.pdf
        link.href = re.sub(r'^(https?://repository\.ubn\.ru\.nl/bitstream/)(\d+.*\.pdf)$', r'\1handle/\2', link.href)
        link.href = re.sub(r'^http://(journal\.nileuniversity.edu\.ng/?.*)', r'https://\1', link.href)
        link.href = re.sub(r'^http://virginialibrariesjournal\.org//articles', r'http://virginialibrariesjournal.org/articles', link.href)
        link.href = re.sub(r'^http://www.(ecologyandsociety.org/.*.pdf)', r'https://www.\1', link.href)

        # preview PDF
        nature_pdf = re.match(r'^https?://www\.nature\.com(/articles/[a-z0-9-]*.pdf)', link.href)
        if nature_pdf:
            reference_pdf = re.sub(r'\.pdf$', '_reference.pdf',  nature_pdf.group(1))
            if reference_pdf in page:
                link.href = reference_pdf

    return link


def _decode_escaped_href(href):
    if re.search(r'\\u[0-9a-fA-F]{4}', href):
        try:
            return href.encode().decode('unicode-escape')
        except UnicodeDecodeError:
            pass

    return href


def get_pdf_from_javascript(page):
    matches = re.findall('"pdfUrl":"(.*?)"', page)
    if matches:
        link = DuckLink(href=_decode_escaped_href(matches[0]), anchor="pdfUrl")
        return link

    matches = re.findall('"exportPdfDownloadUrl": ?"(.*?)"', page)
    if matches:
        link = DuckLink(href=_decode_escaped_href(matches[0]), anchor="exportPdfDownloadUrl")
        return link

    return None


# count the landing page as an OA location but don't use the PDF URL
def _discard_pdf_url(pdf_url, landing_url):

    parsed_pdf_url = urlparse(pdf_url)

    # PDF URLs work but aren't stable
    if parsed_pdf_url.hostname and parsed_pdf_url.hostname.endswith('exlibrisgroup.com') \
            and parsed_pdf_url.query and 'Expires=' in parsed_pdf_url.query:
        return True

    # many papers on the same page
    if landing_url == 'https://www.swarthmore.edu/donna-jo-napoli/publications-available-download':
        return True

    return False
