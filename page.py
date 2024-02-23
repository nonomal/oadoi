#!/usr/bin/python
# -*- coding: utf-8 -*-

import datetime
import gzip
import random
import re

import boto3
import dateutil.parser
import shortuuid
from sqlalchemy import sql, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declared_attr

import oa_page
from app import db
from app import logger
from http_cache import http_get
from oa_local import find_normalized_license
from oa_pmc import query_pmc
from pdf_to_text import convert_pdf_to_txt_pages
from pdf_util import PDFVersion, save_pdf, enqueue_pdf_parsing
from util import clean_url, is_pmc
from webpage import PmhRepoWebpage, PublisherWebpage

DEBUG_BASE = False


class PmhVersionFirstAvailable(db.Model):
    endpoint_id = db.Column(db.Text, primary_key=True)
    pmh_id = db.Column(db.Text, primary_key=True)
    scrape_version = db.Column(db.Text, primary_key=True)
    first_available = db.Column(db.DateTime)
    url = db.Column(db.Text)


class PageBase(db.Model):
    __abstract__ = True

    id = db.Column(db.Text, primary_key=True)
    url = db.Column(db.Text)

    @declared_attr
    def pmh_id(self):
        return db.Column(db.Text, db.ForeignKey("pmh_record.id"))

    @declared_attr
    def endpoint_id(self):
        return db.Column(db.Text, db.ForeignKey("endpoint.id"))

    title = db.Column(db.Text)

    @declared_attr
    def normalized_title(self):
        return db.Column(db.Text, db.ForeignKey("pub.normalized_title"))

    scrape_updated = db.Column(db.DateTime)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_version = db.Column(db.Text)
    scrape_license = db.Column(db.Text)

    record_timestamp = db.Column(db.DateTime)

    error = db.Column(db.Text)
    updated = db.Column(db.DateTime)

    rand = db.Column(db.Numeric)

    @declared_attr
    def endpoint(self):
        return db.relationship(
            'Endpoint',
            lazy='subquery',
            uselist=None,
            cascade="",
            viewonly=True
        )

    @declared_attr
    def pmh_record(self):
        return db.relationship(
            'PmhRecord',
            lazy='subquery',
            uselist=None,
            cascade="",
            viewonly=True
        )

    @property
    def first_available(self):
        if self.scrape_version and self.pmh_record:
            lookup = PmhVersionFirstAvailable.query.filter(
                PmhVersionFirstAvailable.pmh_id == self.pmh_record.bare_pmh_id,
                PmhVersionFirstAvailable.endpoint_id == self.pmh_record.endpoint_id,
                PmhVersionFirstAvailable.scrape_version == self.scrape_version
            ).first()

            if lookup:
                return lookup.first_available.date()

        return self.save_first_version_availability()

    @property
    def is_open(self):
        return self.scrape_metadata_url or self.scrape_pdf_url

    @property
    def is_pmc(self):
        return self.url and is_pmc(self.url)

    @property
    def pmcid(self):
        if not self.is_pmc:
            return None
        matches = re.findall("(pmc\d+)", self.url, re.IGNORECASE)
        if not matches:
            return None
        return matches[0].lower()

    def store_fulltext(self, fulltext_bytes, fulltext_type):
        pass

    def store_landing_page(self, landing_page_markup):
        pass

    def get_pmh_record_url(self):
        return self.endpoint and self.pmh_record and "{}?verb=GetRecord&metadataPrefix={}&identifier={}".format(
            self.endpoint.pmh_url, self.endpoint.metadata_prefix,
            self.pmh_record.bare_pmh_id
        )

    @property
    def repository_display_name(self):
        if self.endpoint and self.endpoint.repo:
            return self.endpoint.repo.display_name()
        else:
            return None

    @property
    def bare_pmh_id(self):
        return self.pmh_record and self.pmh_record.bare_pmh_id

    @property
    def has_no_error(self):
        return self.error is None or self.error == ""

    @property
    def scrape_updated_datetime(self):
        if isinstance(self.scrape_updated, str):
            return datetime.datetime.strptime(self.scrape_updated,
                                              "%Y-%m-%dT%H:%M:%S.%f")
        elif isinstance(self.scrape_updated, datetime.datetime):
            return self.scrape_updated
        else:
            return None

    def not_scraped_in(self, interval):
        return (
                not self.scrape_updated_datetime
                or self.scrape_updated_datetime < (
                        datetime.datetime.now() - interval)
        )

    def scrape_eligible(self):
        return (
                (self.has_no_error or self.not_scraped_in(
                    datetime.timedelta(weeks=1))) and
                (
                        self.pmh_id and "oai:open-archive.highwire.org" not in self.pmh_id) and
                not (
                        self.url and '//hdl.handle.net/10454/' in self.url) and  # https://support.unpaywall.org/a/tickets/22695
                not (self.url and self.url.startswith(
                    'https://biblio.vub.ac.be/vubir/') and self.url.endswith(
                    '.html'))
        )

    def set_info_for_pmc_page(self):
        if not self.pmcid:
            return

        result_list = query_pmc(self.pmcid)
        if not result_list:
            return
        result = result_list[0]
        has_pdf = result.get("hasPDF", None)
        is_author_manuscript = result.get("authMan", None)
        is_open_access = result.get("isOpenAccess", None)
        raw_license = result.get("license", None)

        self.scrape_metadata_url = "http://europepmc.org/articles/{}".format(
            self.pmcid)
        if has_pdf == "Y":
            self.scrape_pdf_url = "http://europepmc.org/articles/{}?pdf=render".format(
                self.pmcid)
            if self.pmcid == 'pmc2126438':
                self.scrape_pdf_url += '#page=8'
        if is_author_manuscript == "Y":
            self.scrape_version = "acceptedVersion"
        else:
            self.scrape_version = "publishedVersion"
        if raw_license:
            self.scrape_license = find_normalized_license(raw_license)
        elif is_open_access == "Y":
            self.scrape_license = "unspecified-oa"

    def scrape(self):
        if not self.scrape_eligible():
            logger.info('refusing to scrape this page')
            return

        self.updated = datetime.datetime.utcnow().isoformat()
        self.scrape_updated = datetime.datetime.utcnow().isoformat()
        self.scrape_pdf_url = None
        self.scrape_metadata_url = None
        self.scrape_license = None
        self.scrape_version = None
        self.error = ""

        if self.pmh_id != oa_page.publisher_equivalent_pmh_id:
            self.scrape_green()
        else:
            self.scrape_publisher_equivalent()

    def scrape_publisher_equivalent(self):
        with PublisherWebpage(url=self.url) as publisher_page:
            publisher_page.scrape_for_fulltext_link()

            if publisher_page.is_open:
                self.scrape_version = publisher_page.open_version_source_string
                self.scrape_pdf_url = publisher_page.scraped_pdf_url
                self.scrape_metadata_url = publisher_page.scraped_open_metadata_url
                self.scrape_license = publisher_page.scraped_license
                if publisher_page.is_open and not publisher_page.scraped_pdf_url:
                    self.scrape_metadata_url = self.url

    def scrape_green(self):
        # handle these special cases, where we compute the pdf rather than looking for it
        if "oai:arXiv.org" in self.pmh_id:
            self.scrape_metadata_url = self.url
            self.scrape_pdf_url = self.url.replace("abs", "pdf")

        if self.is_pmc:
            self.set_info_for_pmc_page()
            return

        # https://ink.library.smu.edu.sg/do/oai/
        if self.endpoint and self.endpoint.id == 'ys9xnlw27yogrfsecedx' and 'ink.library.smu.edu.sg' in self.url:
            if 'viewcontent.cgi?' in self.url:
                return
            if self.pmh_record and find_normalized_license(
                    self.pmh_record.license):
                self.scrape_metadata_url = self.url
                self.set_version_and_license()
                return

        pdf_r = None
        if not self.scrape_pdf_url or not self.scrape_version:
            with PmhRepoWebpage(url=self.url,
                                scraped_pdf_url=self.scrape_pdf_url) as my_webpage:
                if not self.scrape_pdf_url:
                    my_webpage.scrape_for_fulltext_link()
                    self.error += my_webpage.error
                    if my_webpage.is_open:
                        logger.info("** found an open copy! {}".format(
                            my_webpage.fulltext_url))
                        pdf_r = my_webpage.r
                        self.scrape_updated = datetime.datetime.utcnow().isoformat()
                        self.scrape_metadata_url = self.url
                        if my_webpage.scraped_pdf_url:
                            self.scrape_pdf_url = my_webpage.scraped_pdf_url
                        if my_webpage.scraped_open_metadata_url:
                            self.scrape_metadata_url = my_webpage.scraped_open_metadata_url
                        if my_webpage.scraped_license:
                            self.scrape_license = my_webpage.scraped_license
                        if my_webpage.scraped_version:
                            self.scrape_version = my_webpage.scraped_version

                if self.scrape_pdf_url and not self.scrape_version:
                    self.set_version_and_license(r=my_webpage.r)
                elif self.is_open and not self.scrape_version:
                    self.update_with_local_info()

                self.store_fulltext(my_webpage.fulltext_bytes,
                                    my_webpage.fulltext_type)
                self.store_landing_page(my_webpage.page_text)

        if self.scrape_pdf_url and not self.scrape_version:
            with PmhRepoWebpage(url=self.url,
                                scraped_pdf_url=self.scrape_pdf_url,
                                repo_id=self.repo_id) as my_webpage:
                my_webpage.set_r_for_pdf()
                pdf_r = my_webpage.r
                self.set_version_and_license(r=my_webpage.r)

        if self.is_open and not self.scrape_version:
            self.scrape_version = self.default_version()

        # associate certain landing page URLs with PDFs
        # https://repository.uantwerpen.be
        if self.endpoint and self.endpoint.id == 'mmv3envg3kaaztya9tmo':
            if self.scrape_pdf_url and self.scrape_pdf_url == self.scrape_metadata_url and self.pmh_record:
                logger.info('looking for landing page for {}'.format(
                    self.scrape_pdf_url))
                landing_urls = [u for u in self.pmh_record.urls if
                                'hdl.handle.net' in u]
                if len(landing_urls) == 1:
                    logger.info(
                        'trying landing page {}'.format(landing_urls[0]))

                    try:
                        if http_get(landing_urls[0]).status_code == 200:
                            self.scrape_metadata_url = landing_urls[0]
                    except:
                        pass

                    if self.scrape_metadata_url:
                        logger.info('set landing page {}'.format(
                            self.scrape_metadata_url))

        # https://lirias.kuleuven.be
        if (self.endpoint
                and self.endpoint.id == 'ycf3gzxeiyuw3jqwjmx3'
                and self.scrape_pdf_url == self.scrape_metadata_url
                and self.scrape_pdf_url and 'lirias.kuleuven.be' in self.scrape_pdf_url
        ):
            if self.pmh_record and self.pmh_record.bare_pmh_id and 'oai:lirias2repo.kuleuven.be:' in self.pmh_record.bare_pmh_id:
                self.scrape_metadata_url = 'https://lirias.kuleuven.be/handle/{}'.format(
                    self.pmh_record.bare_pmh_id.replace(
                        'oai:lirias2repo.kuleuven.be:', '')
                )
        if self.scrape_metadata_url:
            self.scrape_metadata_url = clean_url(self.scrape_metadata_url)

        if self.scrape_pdf_url:
            self.scrape_pdf_url = clean_url(self.scrape_pdf_url)

        if isinstance(self, PageNew) and self.scrape_version and self.doi:
            logger.debug(f'Saving {self.scrape_version} PDF of DOI - {self.doi}')
            self.save_pdf(PDFVersion.from_version_str(self.scrape_version),
                          pdf_r=pdf_r)

    def pmc_first_available_date(self):
        if self.pmcid:
            pmc_result_list = query_pmc(self.pmcid)
            if pmc_result_list:
                pmc_result = pmc_result_list[0]
                received_date = pmc_result.get("fullTextReceivedDate", None)
                if received_date:
                    try:
                        return datetime.datetime.strptime(received_date,
                                                          '%Y-%m-%d').date()
                    except Exception:
                        return None

        return None

    def save_first_version_availability(self):
        if not self.scrape_version:
            return None

        first_available = None

        if isinstance(self.record_timestamp, str):
            first_available = dateutil.parser.parse(self.record_timestamp)
        elif isinstance(self.record_timestamp, datetime.datetime):
            first_available = self.record_timestamp.date()
        elif isinstance(self.record_timestamp, datetime.date):
            first_available = self.record_timestamp

        if self.pmcid:
            first_available = self.pmc_first_available_date()

        if (self.endpoint and self.endpoint.id and
                self.pmh_record and self.pmh_record.bare_pmh_id and
                self.url and
                first_available):
            stmt = sql.text('''
                insert into pmh_version_first_available
                (endpoint_id, pmh_id, url, scrape_version, first_available) values
                (:endpoint_id, :pmh_id, :url, :scrape_version, :first_available)
                on conflict do nothing
            ''').bindparams(
                endpoint_id=self.endpoint.id,
                pmh_id=self.pmh_record.bare_pmh_id,
                url=self.url,
                scrape_version=self.scrape_version,
                first_available=first_available
            )
            db.session.execute(stmt)

        return first_available

    def default_version(self):
        if self.endpoint and self.endpoint.policy_promises_no_submitted:
            return "acceptedVersion"
        elif self.bare_pmh_id and 'oai:library.wur.nl:wurpubs' in self.bare_pmh_id:
            return "acceptedVersion"
        else:
            return "submittedVersion"

    def update_with_local_info(self):
        scrape_version_old = self.scrape_version
        scrape_license_old = self.scrape_license

        # if this repo has told us they will never have submitted, set default to be accepted
        if self.endpoint and self.endpoint.policy_promises_no_submitted and self.scrape_version != "publishedVersion":
            self.scrape_version = "acceptedVersion"

        # now look at the pmh record
        if self.pmh_record:
            # trust accepted in a variety of formats
            accepted_patterns = [
                re.compile(r"accepted.?version",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(r"version.?accepted",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(r"accepted.?manuscript",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(r"<dc:type>peer.?reviewed</dc:type>",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(
                    r"<dc:description>Refereed/Peer-reviewed</dc:description>",
                    re.IGNORECASE | re.MULTILINE | re.DOTALL),
            ]
            for pattern in accepted_patterns:
                if pattern.findall(self.pmh_record.api_raw):
                    self.scrape_version = "acceptedVersion"
            # print u"version for is {}".format(self.scrape_version)

            # trust a strict version of published version
            published_patterns = [
                re.compile(r"<dc:type>.*publishedVersion</dc:type>",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(
                    r"<dc:type\.version>.*publishedVersion</dc:type\.version>",
                    re.IGNORECASE | re.MULTILINE | re.DOTALL),
                re.compile(r"<free_to_read>.*published.*</free_to_read>",
                           re.IGNORECASE | re.MULTILINE | re.DOTALL)
            ]
            for published_pattern in published_patterns:
                if published_pattern.findall(self.pmh_record.api_raw):
                    self.scrape_version = "publishedVersion"

            # get license if it is in pmh record
            rights_pattern = re.compile(r"<dc:rights>(.*)</dc:rights>",
                                        re.IGNORECASE | re.MULTILINE | re.DOTALL)
            rights_matches = rights_pattern.findall(self.pmh_record.api_raw)
            rights_license_pattern = re.compile(
                r"<dc:rights\.license>(.*)</dc:rights\.license>",
                re.IGNORECASE | re.MULTILINE | re.DOTALL)
            rights_matches.extend(
                rights_license_pattern.findall(self.pmh_record.api_raw))

            for rights_text in rights_matches:
                open_license = find_normalized_license(rights_text)
                # only overwrite it if there is one, so doesn't overwrite anything scraped
                if open_license:
                    self.scrape_license = open_license

            self.scrape_license = _scrape_license_override().get(
                self.pmh_record.bare_pmh_id, self.scrape_license)

        if self.scrape_pdf_url and re.search(r'^https?://rke\.abertay\.ac\.uk',
                                             self.scrape_pdf_url):
            if re.search(r'Publishe[dr]_?\d\d\d\d\.pdf$', self.scrape_pdf_url):
                self.scrape_version = "publishedVersion"
            if re.search(r'\d\d\d\d_?Publishe[dr].pdf$', self.scrape_pdf_url):
                self.scrape_version = "publishedVersion"

        if self.pmh_record:
            self.scrape_version = _scrape_version_override().get(
                self.pmh_record.bare_pmh_id, self.scrape_version)

        if scrape_version_old != self.scrape_version or scrape_license_old != self.scrape_license:
            self.updated = datetime.datetime.utcnow().isoformat()
            print("based on OAI-PMH metadata, updated {} {} for {} {}".format(
                self.scrape_version, self.scrape_license, self.url, self.id))
            return True

        # print u"based on metadata, assuming {} {} for {} {}".format(self.scrape_version, self.scrape_license, self.url, self.id)

        return False

    # use standards from https://wiki.surfnet.nl/display/DRIVERguidelines/Version+vocabulary
    # submittedVersion, acceptedVersion, publishedVersion
    def set_version_and_license(self, r=None):
        self.updated = datetime.datetime.utcnow().isoformat()

        if self.is_pmc:
            self.set_info_for_pmc_page()
            return

        # set as default
        self.scrape_version = self.default_version()

        self.update_with_local_info()

        # now try to see what we can get out of the pdf itself
        version_is_from_strict_metadata = self.pmh_record and self.pmh_record.api_raw and re.compile(
            r"<dc:type>{}</dc:type>".format(self.scrape_version),
            re.IGNORECASE | re.MULTILINE | re.DOTALL
        ).findall(self.pmh_record.api_raw)

        if version_is_from_strict_metadata or not r:
            logger.info(
                "before scrape returning {} with scrape_version: {}, license {}".format(
                    self.url, self.scrape_version, self.scrape_license))
            return

        try:
            # http://crossmark.dyndns.org/dialog/?doi=10.1016/j.jml.2012 at http://dspace.mit.edu/bitstream/1721.1/102417/1/Gibson_The%20syntactic.pdf
            if re.findall("crossmark\.[^/]*\.org/", r.text_big(),
                          re.IGNORECASE):
                self.scrape_version = "publishedVersion"

            pages_text = convert_pdf_to_txt_pages(r, max_pages=25)
            first_page_text = pages_text and pages_text[0]
            text = '\n'.join(pages_text)

            if text and self.scrape_version != "publishedVersion" and not version_is_from_strict_metadata:
                patterns = [
                    re.compile(r"©.?\d{4}", re.UNICODE),
                    re.compile(r"© The Author\(s\),? \d{4}", re.UNICODE),
                    re.compile(r"\(C\).?\d{4}", re.IGNORECASE),
                    re.compile(r"copyright.{0,6}\d{4}", re.IGNORECASE),
                    re.compile(
                        r"received.{0,100}revised.{0,100}accepted.{0,100}publication",
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(r"all rights reserved", re.IGNORECASE),
                    re.compile(
                        r"This article is distributed under the terms of the Creative Commons",
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(
                        r"This article is licensed under a Creative Commons",
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(r"this is an open access article",
                               re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(
                        r"This article is brought to you for free and open access by Works.",
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                ]

                for pattern in patterns:
                    if pattern.findall(text):
                        logger.info(
                            'found {}, decided PDF is published version'.format(
                                pattern.pattern))
                        self.scrape_version = "publishedVersion"

            if text and self.scrape_version != 'acceptedVersion':
                patterns = [
                    re.compile(
                        r'This is a post-peer-review, pre-copyedit version',
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(
                        r'This is the peer reviewed version of the following article',
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(
                        r'The present manuscript as of \d\d \w+ \d\d\d\d has been accepted',
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(
                        r'Post-peer-review, pre-copyedit version of accepted manuscript',
                        re.IGNORECASE | re.MULTILINE | re.DOTALL),
                    re.compile(r'This is a "Post-Print" accepted manuscript',
                               re.IGNORECASE | re.MULTILINE | re.DOTALL),
                ]

                for pattern in patterns:
                    if pattern.findall(text):
                        logger.info(
                            'found {}, decided PDF is accepted version'.format(
                                pattern.pattern))
                        self.scrape_version = "acceptedVersion"

                if r and r.url and '61RMIT_INST' in r.url:
                    if 'Version: Accepted' in text:
                        logger.info(
                            'found Version: Accepted, decided PDF is accepted version')
                        self.scrape_version = "acceptedVersion"

                if first_page_text:
                    if 'Version: Accepted' in first_page_text:
                        logger.info(
                            'found Version: Accepted, decided PDF is accepted version')
                        self.scrape_version = "acceptedVersion"
                    if re.compile(
                            r"Document Version\s+Final published version.",
                            re.IGNORECASE | re.MULTILINE | re.DOTALL).findall(
                        first_page_text):
                        logger.info(
                            'found Document Version - Final published version, decided PDF is published')
                        self.scrape_version = "publishedVersion"
                    if re.compile(r"Document Version:\s*Peer reviewed version",
                                  re.IGNORECASE | re.MULTILINE | re.DOTALL).findall(
                        first_page_text):
                        logger.info(
                            'found Document Version: Peer reviewed version, decided PDF is accepted')
                        self.scrape_version = "acceptedVersion"

                heading_text = text[0:50].lower()
                accepted_headings = [
                    "final accepted version",
                    "accepted manuscript",
                ]

                for heading in accepted_headings:
                    if heading in heading_text:
                        logger.info(
                            'found {} in heading, decided PDF is accepted version'.format(
                                heading))
                        self.scrape_version = "acceptedVersion"
                        break

            if not self.scrape_license:
                open_license = find_normalized_license(text)
                if open_license:
                    logger.info('found license in PDF: {}'.format(open_license))
                    self.scrape_license = open_license

        except Exception as e:
            logger.exception(
                "exception in convert_pdf_to_txt for {}".format(self.url))
            self.error += "Exception doing convert_pdf_to_txt!"
            logger.info(self.error)

        if self.pmh_record:
            self.scrape_version = _scrape_version_override().get(
                self.pmh_record.bare_pmh_id, self.scrape_version)

        logger.info(
            "scrape returning {} with scrape_version: {}, license {}".format(
                self.url, self.scrape_version, self.scrape_license))

    def __repr__(self):
        return "<PageBase ( {} ) {}>".format(self.pmh_id, self.url)


class LandingPageArchiveKeyLookup(db.Model):
    __tablename__ = 'repo_page_landing_page_key_lookup'

    id = db.Column(db.Text, db.ForeignKey('page_new.id'), primary_key=True)
    key = db.Column(db.Text)


class FulltextArchiveKeyLookup(db.Model):
    __tablename__ = 'repo_page_fulltext_key_lookup'

    id = db.Column(db.Text, db.ForeignKey('page_new.id'), primary_key=True)
    key = db.Column(db.Text)


LANDING_PAGE_ARCHIVE_BUCKET = 'unpaywall-worksdb-repo-landing-page'
FULLTEXT_PDF_ARCHIVE_BUCKET = 'unpaywall-tier-2-fulltext'


class PageNew(PageBase):
    repo_id = db.Column(db.Text)  # delete once endpoint_id is populated
    doi = db.Column(db.Text, db.ForeignKey("pub.id"))
    authors = db.Column(JSONB)

    num_pub_matches = db.Column(db.Numeric)
    match_type = db.Column(db.Text)

    landing_page_archive_key = db.relationship(
        'LandingPageArchiveKeyLookup',
        lazy='subquery',
        uselist=False,
        foreign_keys='LandingPageArchiveKeyLookup.id'
    )

    fulltext_pdf_archive_key = db.relationship(
        'FulltextArchiveKeyLookup',
        lazy='subquery',
        uselist=False,
        foreign_keys='FulltextArchiveKeyLookup.id'
    )

    __mapper_args__ = {
        "polymorphic_on": match_type,
        "polymorphic_identity": "page_new"
    }

    def __init__(self, **kwargs):
        self.id = shortuuid.uuid()[0:20]
        self.error = ""
        self.rand = random.random()
        self.updated = datetime.datetime.utcnow().isoformat()
        super(PageNew, self).__init__(**kwargs)

    def store_fulltext(self, fulltext_bytes, fulltext_type):
        if fulltext_bytes and (
                self.num_pub_matches is None or self.num_pub_matches < 1):
            try:
                if not self.fulltext_pdf_archive_key:
                    self.fulltext_pdf_archive_key = FulltextArchiveKeyLookup(
                        id=self.id, key=self.id)

                logger.info(
                    f'saving {len(fulltext_bytes)} {fulltext_type} bytes to {self.fulltext_pdf_archive_url()}')
                client = boto3.client('s3', verify=False)
                client.put_object(
                    Body=gzip.compress(fulltext_bytes),
                    Bucket=FULLTEXT_PDF_ARCHIVE_BUCKET,
                    Key=self.fulltext_pdf_archive_key.key
                )
            except Exception as e:
                logger.error(f'failed to save fulltext bytes: {e}')

    def store_landing_page(self, landing_page_markup):
        if landing_page_markup:
            try:
                if not self.landing_page_archive_key:
                    self.landing_page_archive_key = LandingPageArchiveKeyLookup(
                        id=self.id, key=f'{self.id}.gz')

                logger.info(
                    f'saving {len(landing_page_markup)} characters to {self.landing_page_archive_url()}')
                client = boto3.client('s3', verify=False)
                client.put_object(
                    Body=gzip.compress(landing_page_markup.encode('utf-8')),
                    Bucket=LANDING_PAGE_ARCHIVE_BUCKET,
                    Key=self.landing_page_archive_key.key
                )
            except Exception as e:
                logger.error(f'failed to save landing page text: {e}')

    def landing_page_archive_url(self):
        if not self.landing_page_archive_key:
            return None
        else:
            return f's3://{LANDING_PAGE_ARCHIVE_BUCKET}/{self.landing_page_archive_key.key}'

    def fulltext_pdf_archive_url(self):
        if not self.fulltext_pdf_archive_key:
            return None
        else:
            return f's3://{FULLTEXT_PDF_ARCHIVE_BUCKET}/{self.fulltext_pdf_archive_key.key}'

    def scrape_if_matches_pub(self):
        self.num_pub_matches = self.query_for_num_pub_matches()

        if self.num_pub_matches > 0 and self.scrape_eligible():
            return self.scrape()

    def enqueue_scrape_if_matches_pub(self):
        self.num_pub_matches = self.query_for_num_pub_matches()

        if self.num_pub_matches > 0 and self.scrape_eligible():
            stmt = sql.text(
                'insert into page_green_scrape_queue (id, finished, endpoint_id) values (:id, :finished, :endpoint_id) on conflict do nothing'
            ).bindparams(id=self.id, finished=self.scrape_updated,
                         endpoint_id=self.endpoint_id)
            db.session.execute(stmt)

    def __repr__(self):
        return "<PageNew ( {} ) {}>".format(self.pmh_id, self.url)

    def to_dict(self, include_id=True):
        response = {
            "oaipmh_id": self.pmh_record and self.pmh_record.bare_pmh_id,
            "oaipmh_record_timestamp": self.record_timestamp.isoformat(),
            "pdf_url": self.scrape_pdf_url,
            "title": self.title,
            "version": self.scrape_version,
            "license": self.scrape_license,
            "oaipmh_api_url": self.get_pmh_record_url()
        }
        if include_id:
            response["id"] = self.id
        return response

    def save_pdf(self, version: PDFVersion, pdf_r=None):
        if not pdf_r:
            pdf_r = http_get(self.scrape_pdf_url, ask_slowly=True, stream=False)
        pdf_content = None
        if hasattr(pdf_r, 'content_big'):
            pdf_content = pdf_r.content_big()
        elif hasattr(pdf_r, 'content'):
            pdf_content = pdf_r.content
        if pdf_content:
            save_pdf(self.doi, pdf_content, version)
        enqueue_pdf_parsing(self.doi, version)


class Page(db.Model):
    url = db.Column(db.Text, primary_key=True)
    id = db.Column(db.Text, db.ForeignKey("pmh_record.id"))
    source = db.Column(db.Text)
    doi = db.Column(db.Text, db.ForeignKey("pub.id"))
    title = db.Column(db.Text)
    normalized_title = db.Column(db.Text, db.ForeignKey("pub.normalized_title"))
    authors = db.Column(JSONB)

    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_version = db.Column(db.Text)
    scrape_license = db.Column(db.Text)

    error = db.Column(db.Text)
    updated = db.Column(db.DateTime)

    started = db.Column(db.DateTime)
    finished = db.Column(db.DateTime)
    rand = db.Column(db.Numeric)

    def __init__(self, **kwargs):
        self.error = ""
        self.updated = datetime.datetime.utcnow().isoformat()
        super(self.__class__, self).__init__(**kwargs)

    @property
    def first_available(self):
        return None

    @property
    def pmh_id(self):
        return self.id

    @property
    def bare_pmh_id(self):
        return self.id

    @property
    def is_open(self):
        return self.scrape_metadata_url or self.scrape_pdf_url

    @property
    def is_pmc(self):
        if not self.url:
            return False
        if "ncbi.nlm.nih.gov/pmc" in self.url:
            return True
        if "europepmc.org/articles/" in self.url:
            return True
        return False

    @property
    def repo_id(self):
        if not self.pmh_id or not ":" in self.pmh_id:
            return None
        return self.pmh_id.split(":")[1]

    @property
    def endpoint_id(self):
        if not self.pmh_id or not ":" in self.pmh_id:
            return None
        return self.pmh_id.split(":")[1]

    @property
    def pmcid(self):
        if not self.is_pmc:
            return None
        return self.url.rsplit("/", 1)[1].lower()

    @property
    def is_preprint_repo(self):
        preprint_url_fragments = [
            "precedings.nature.com",
            "10.15200/winn.",
            "/peerj.preprints",
            ".figshare.",
            "10.1101/",  # biorxiv
            "10.15363/"  # thinklab
        ]
        for url_fragment in preprint_url_fragments:
            if self.url and url_fragment in self.url.lower():
                return True
        return False

    @property
    def repository_display_name(self):
        return self.repo_id

    def update_with_local_info(self):
        pass

    # examples
    # https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMC3039489&resulttype=core&format=json&tool=oadoi
    # https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMC3606428&resulttype=core&format=json&tool=oadoi
    def set_info_for_pmc_page(self):
        if not self.pmcid:
            return

        url_template = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={}&resulttype=core&format=json&tool=oadoi"
        url = url_template.format(self.pmcid)

        # try:
        r = http_get(url)
        data = r.json()
        result_list = data["resultList"]["result"]
        if not result_list:
            return
        result = result_list[0]
        has_pdf = result.get("hasPDF", None)
        is_author_manuscript = result.get("authMan", None)
        is_open_access = result.get("isOpenAccess", None)
        raw_license = result.get("license", None)

        self.scrape_metadata_url = "http://europepmc.org/articles/{}".format(
            self.pmcid)
        if has_pdf == "Y":
            self.scrape_pdf_url = "http://europepmc.org/articles/{}?pdf=render".format(
                self.pmcid)
        if is_author_manuscript == "Y":
            self.scrape_version = "acceptedVersion"
        else:
            self.scrape_version = "publishedVersion"
        if raw_license:
            self.scrape_license = find_normalized_license(raw_license)
        elif is_open_access == "Y":
            self.scrape_license = "unspecified-oa"

        # except Exception as e:
        #     self.error += u"Exception in set_info_for_pmc_page"
        #     logger.info(u"Exception in set_info_for_pmc_page")

    def __repr__(self):
        return "<Page ( {} ) {} doi:{} '{}...'>".format(self.pmh_id, self.url,
                                                        self.doi,
                                                        self.title[0:20])


# legacy, just used for matching
class BaseMatch(db.Model):
    id = db.Column(db.Text, primary_key=True)
    base_id = db.Column(db.Text)
    doi = db.Column(db.Text, db.ForeignKey('pub.id'))
    url = db.Column(db.Text)
    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_version = db.Column(db.Text)
    scrape_license = db.Column(db.Text)
    updated = db.Column(db.DateTime)

    @property
    def is_open(self):
        return self.scrape_metadata_url or self.scrape_pdf_url


def _scrape_version_override():
    return {
        'oai:dspace.cvut.cz:10467/86163': 'submittedVersion',
        'oai:repository.arizona.edu:10150/633848': 'acceptedVersion',
        'oai:archive.ugent.be:6914822': 'acceptedVersion',
        'oai:serval.unil.ch:BIB_E033703283B2': 'acceptedVersion',
        'oai:serval.unil.ch:BIB_3108959306C9': 'acceptedVersion',
        'oai:serval.unil.ch:BIB_08C9BAB31C2E': 'acceptedVersion',
        'oai:serval.unil.ch:BIB_E8CC2511C152': 'acceptedVersion',
        'oai:HAL:hal-01924005v1': 'acceptedVersion',
        'oai:serval.unil.ch:BIB_FC320764865F': 'publishedVersion',
        'oai:serval.unil.ch:BIB_12B5A0826BD9': 'acceptedVersion',
        'oai:upcommons.upc.edu:2117/115471': 'acceptedVersion',
        'oai:zenodo.org:4267175': 'acceptedVersion',
        'oai:europepmc.org:7435056': 'acceptedVersion',
    }


def _scrape_license_override():
    return {
        'oai:academiccommons.columbia.edu:10.7916/D8D80PCQ': None,
    }


class RepoPage(PageNew):
    __tablename__ = None
    __mapper_args__ = {"polymorphic_identity": "any"}

    match_title = db.Column(db.Boolean)
    match_doi = db.Column(db.Boolean)

    def __init__(self, **kwargs):
        self.match_title = False
        self.match_doi = False
        super(RepoPage, self).__init__(**kwargs)

    def __repr__(self):
        return "<RepoPage ( {} ) {} match_title: {}, match_doi: {}>".format(
            self.pmh_id, self.url, self.match_title, self.match_doi
        )

    def to_dict(self, include_id=True):
        response = {
            "oaipmh_id": self.pmh_record and self.pmh_record.bare_pmh_id,
            "oaipmh_record_timestamp": self.record_timestamp.isoformat(),
            "pdf_url": self.scrape_pdf_url,
            "title": self.title,
            "version": self.scrape_version,
            "license": self.scrape_license,
            "oaipmh_api_url": self.get_pmh_record_url(),
            "match_title": self.match_title,
            "match_doi": self.match_doi
        }
        if include_id:
            response["id"] = self.id
        return response

    def query_for_num_pub_matches(self):
        from pmh_record import title_is_too_common
        from pmh_record import title_is_too_short
        from pub import Pub

        if self.match_title and not (
                title_is_too_common(self.normalized_title)
                or title_is_too_short(self.normalized_title)
        ):
            title_match_clause = Pub.normalized_title == self.normalized_title
        else:
            title_match_clause = None

        if self.match_doi and self.doi:
            doi_match_clause = Pub.id == self.doi
        else:
            doi_match_clause = None

        if doi_match_clause is not None and title_match_clause is not None:
            match_clause = or_(title_match_clause, doi_match_clause)
        elif title_match_clause is not None:
            match_clause = title_match_clause
        elif doi_match_clause is not None:
            match_clause = doi_match_clause
        else:
            match_clause = None

        if match_clause is not None:
            return db.session.query(Pub.id).filter(match_clause).count()
        else:
            return 0


class PageGreenScrapeQueue(db.Model):
    id = db.Column(db.Text, db.ForeignKey('page_new.id'), primary_key=True)
    endpoint_id = db.Column(db.Text)
