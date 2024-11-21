import datetime
import gzip
import json
import random
import re
import urllib.parse
from collections import Counter
from collections import OrderedDict
from collections import defaultdict
from enum import Enum
from threading import Thread

import boto3
import dateutil.parser
import requests
from dateutil.relativedelta import relativedelta
from lxml import etree
from psycopg2.errors import UniqueViolation
from sqlalchemy import orm, sql, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

import oa_evidence
import oa_local
import oa_manual
import oa_page
import page
from app import db
from app import logger
from const import LANDING_PAGE_ARCHIVE_BUCKET, PDF_ARCHIVE_BUCKET
from convert_http_to_https import fix_url_scheme
from http_cache import get_session_id
from journal import Journal
from open_location import OpenLocation, validate_pdf_urls, OAStatus, \
    oa_status_sort_key
from pdf_url import PdfUrl
from pdf_util import save_pdf
from pmh_record import is_known_mismatch
from pmh_record import title_is_too_common
from pmh_record import title_is_too_short
from recordthresher.record import RecordthresherParentRecord
from recordthresher.record_maker import CrossrefRecordMaker
from recordthresher.record_maker.parseland_record_maker import \
    ParselandRecordMaker
from recordthresher.record_maker import PmhRecordMaker
from recordthresher.record_maker.pdf_record_maker import PDFRecordMaker
from reported_noncompliant_copies import reported_noncompliant_url_fragments
from util import NoDoiException
from util import is_pmc, clamp, clean_doi, normalize_doi
from convert_http_to_https import fix_url_scheme
from util import normalize
from util import normalize_title
from util import safe_commit
from webpage import PublisherWebpage

s2_endpoint_id = 'trmgzrn8eq4yx7ddvmzs'

s3 = boto3.client('s3', verify=False)


def build_new_pub(doi, crossref_api):
    my_pub = Pub(id=doi, crossref_api_raw_new=crossref_api)
    my_pub.title = my_pub.crossref_title
    my_pub.normalized_title = normalize_title(my_pub.title)
    return my_pub


def add_new_pubs(pubs_to_commit):
    if not pubs_to_commit:
        return []

    pubs_indexed_by_id = dict((my_pub.id, my_pub) for my_pub in pubs_to_commit)
    ids_already_in_db = [
        id_tuple[0] for id_tuple in db.session.query(Pub.id).filter(
            Pub.id.in_(list(pubs_indexed_by_id.keys()))).all()
    ]
    pubs_to_add_to_db = []

    for (pub_id, my_pub) in pubs_indexed_by_id.items():
        if pub_id in ids_already_in_db:
            # merge if we need to
            pass
        else:
            pubs_to_add_to_db.append(my_pub)
            # logger.info(u"adding new pub {}".format(my_pub.id))

    if pubs_to_add_to_db:
        logger.info("adding {} pubs".format(len(pubs_to_add_to_db)))
        db.session.add_all(pubs_to_add_to_db)
        safe_commit(db)
        db.session.execute(
            text(
                '''
                insert into recordthresher.doi_record_queue (doi, updated) (
                    select id, (crossref_api_raw_new->'indexed'->>'date-time')::timestamp without time zone from pub
                    where id = any (:dois)
                ) on conflict do nothing
                '''
            ).bindparams(dois=[p.id for p in pubs_to_add_to_db])
        )
        safe_commit(db)
    return pubs_to_add_to_db


def call_targets_in_parallel(targets):
    if not targets:
        return

    # logger.info(u"calling", targets)
    threads = []
    for target in targets:
        process = Thread(target=target, args=[])
        process.start()
        threads.append(process)
    for process in threads:
        try:
            process.join(timeout=60 * 10)
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(
                "thread Exception {} in call_targets_in_parallel. continuing.".format(
                    e))
    # logger.info(u"finished the calls to {}".format(targets))


def call_args_in_parallel(target, args_list):
    # logger.info(u"calling", targets)
    threads = []
    for args in args_list:
        process = Thread(target=target, args=args)
        process.start()
        threads.append(process)
    for process in threads:
        try:
            process.join(timeout=60 * 10)
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(
                "thread Exception {} in call_args_in_parallel. continuing.".format(
                    e))
    # logger.info(u"finished the calls to {}".format(targets))


def lookup_product_by_doi(doi):
    biblio = {"doi": doi}
    return lookup_product(**biblio)


def lookup_product(**biblio):
    my_pub = None
    if "doi" in biblio and biblio["doi"]:
        doi = normalize_doi(biblio["doi"])

        # map unregistered JSTOR DOIs to real articles
        # for example https://www.jstor.org/stable/2244328?seq=1 says 10.2307/2244328 on the page
        # but https://doi.org/10.2307/2244328 goes nowhere and the article is at https://doi.org/10.1214/aop/1176990626

        jstor_overrides = {
            '10.2307/2244328': '10.1214/aop/1176990626',
            # https://www.jstor.org/stable/2244328
            '10.2307/25151720': '10.1287/moor.1060.0190',
            # https://www.jstor.org/stable/25151720
            '10.2307/2237638': '10.1214/aoms/1177704711',
            # https://www.jstor.org/stable/2237638
        }

        other_overrides = {
            # these seem to be the same thing but the first one doesn't work
            # https://api.crossref.org/v1/works/http://dx.doi.org/10.3402/qhw.v1i3.4932
            # https://api.crossref.org/v1/works/http://dx.doi.org/10.1080/17482620600881144
            '10.3402/qhw.v1i3.4932': '10.1080/17482620600881144',
        }

        doi = jstor_overrides.get(
            doi,
            other_overrides.get(doi, doi)
        )

        my_pub = Pub.query.get(doi)

        if not my_pub:
            # try cleaning DOI further
            doi = clean_doi(doi)
            my_pub = Pub.query.get(doi)
            if not my_pub:
                raise NoDoiException

    my_pub.reset_vars()
    return my_pub


def refresh_pub(my_pub, do_commit=False):
    my_pub.run_with_hybrid()
    db.session.merge(my_pub)
    if do_commit:
        safe_commit(db)
    return my_pub


def thread_result_wrapper(func, args, res):
    res.append(func(*args))


# get rid of this when we get rid of POST endpoint
# for now, simplify it so it just calls the single endpoint
def get_pubs_from_biblio(biblios, run_with_hybrid=False):
    returned_pubs = []
    for biblio in biblios:
        returned_pubs.append(
            get_pub_from_biblio(biblio, run_with_hybrid=run_with_hybrid))
    return returned_pubs


def get_pub_from_biblio(biblio, run_with_hybrid=False, skip_all_hybrid=False,
                        recalculate=True):
    my_pub = lookup_product(**biblio)
    if run_with_hybrid:
        my_pub.run_with_hybrid()
        safe_commit(db)
    elif recalculate:
        my_pub.recalculate()
    return my_pub


def max_pages_from_one_repo(endpoint_ids):
    endpoint_id_counter = Counter(endpoint_ids)
    most_common = endpoint_id_counter.most_common(1)
    if most_common:
        return most_common[0][1]
    return 0


def get_citeproc_date(year=0, month=1, day=1):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def csv_dict_from_response_dict(data):
    if not data:
        return None

    response = defaultdict(str)
    response["doi"] = data.get("doi", None)
    response["doi_url"] = data.get("doi_url", None)
    response["is_oa"] = data.get("is_oa", None)
    response["oa_status"] = data.get("oa_status", None)
    response["genre"] = data.get("genre", None)
    response["is_paratext"] = data.get("is_paratext", None)
    response["journal_name"] = data.get("journal_name", None)
    response["journal_issns"] = data.get("journal_issns", None)
    response["journal_issn_l"] = data.get("journal_issn_l", None)
    response["journal_is_oa"] = data.get("journal_is_oa", None)
    response["journal_is_in_doaj"] = data.get("journal_is_in_doaj", None)
    response["publisher"] = data.get("publisher", None)
    response["published_date"] = data.get("published_date", None)
    response["data_standard"] = data.get("data_standard", None)

    best_location_data = data.get("best_oa_location", None)
    if not best_location_data:
        best_location_data = defaultdict(str)
    response["best_oa_url"] = best_location_data.get("url", "")
    response["best_oa_url_is_pdf"] = best_location_data.get("url_for_pdf",
                                                            "") != ""
    response["best_oa_evidence"] = best_location_data.get("evidence", None)
    response["best_oa_host"] = best_location_data.get("host_type", None)
    response["best_oa_version"] = best_location_data.get("version", None)
    response["best_oa_license"] = best_location_data.get("license", None)

    return response


def build_crossref_record(data):
    if not data:
        return None

    record = {}

    simple_fields = [
        "publisher",
        "subject",
        "link",
        "license",
        "funder",
        "type",
        "update-to",
        "clinical-trial-number",
        "ISSN",  # needs to be uppercase
        "ISBN",  # needs to be uppercase
        "alternative-id"
    ]

    for field in simple_fields:
        if field in data:
            record[field.lower()] = data[field]

    if "title" in data:
        if isinstance(data["title"], str):
            record["title"] = data["title"]
        else:
            if data["title"]:
                record["title"] = data["title"][0]  # first one
        if "title" in record and record["title"]:
            record["title"] = re.sub("\s+", " ", record["title"])

    if "container-title" in data:
        record["all_journals"] = data["container-title"]
        if isinstance(data["container-title"], str):
            record["journal"] = data["container-title"]
        else:
            if data["container-title"]:
                record["journal"] = data["container-title"][-1]  # last one
        # get rid of leading and trailing newlines
        if record.get("journal", None):
            record["journal"] = record["journal"].strip()

    if "author" in data:
        # record["authors_json"] = json.dumps(data["author"])
        record["all_authors"] = data["author"]
        if data["author"]:
            first_author = data["author"][0]
            if first_author and "family" in first_author:
                record["first_author_lastname"] = first_author["family"]
            for author in record["all_authors"]:
                if author and "affiliation" in author and not author.get(
                        "affiliation", None):
                    del author["affiliation"]

    if "issued" in data:
        # record["issued_raw"] = data["issued"]
        try:
            if "raw" in data["issued"]:
                record["year"] = int(data["issued"]["raw"])
            elif "date-parts" in data["issued"]:
                record["year"] = int(data["issued"]["date-parts"][0][0])
                date_parts = data["issued"]["date-parts"][0]
                pubdate = get_citeproc_date(*date_parts)
                if pubdate:
                    record["pubdate"] = pubdate.isoformat()
        except (IndexError, TypeError):
            pass

    if "deposited" in data:
        try:
            record["deposited"] = data["deposited"]["date-time"]
        except (IndexError, TypeError):
            pass

    record["added_timestamp"] = datetime.datetime.utcnow().isoformat()
    return record


class PmcidPublishedVersionLookup(db.Model):
    pmcid = db.Column(db.Text, db.ForeignKey('pmcid_lookup.pmcid'),
                      primary_key=True)


class PmcidLookup(db.Model):
    doi = db.Column(db.Text, db.ForeignKey('pub.id'), primary_key=True)
    pmcid = db.Column(db.Text)
    release_date = db.Column(db.Text)

    pmcid_pubished_version_link = db.relationship(
        'PmcidPublishedVersionLookup',
        lazy='subquery',
        viewonly=True,
        backref=db.backref("pmcid_lookup", lazy="subquery"),
        foreign_keys="PmcidPublishedVersionLookup.pmcid"
    )

    @property
    def version(self):
        if self.pmcid_pubished_version_link:
            return "publishedVersion"
        return "acceptedVersion"


class IssnlLookup(db.Model):
    __tablename__ = 'openalex_issn_to_issnl'

    issn = db.Column(db.Text, primary_key=True)
    issn_l = db.Column(db.Text)
    journal_id = db.Column(db.Text)


class JournalOaStartYear(db.Model):
    __tablename__ = 'journal_oa_start_year_patched'

    issn_l = db.Column(db.Text, primary_key=True)
    title = db.Column(db.Text)
    oa_year = db.Column(db.Integer)


class S2Lookup(db.Model):
    __tablename__ = 'semantic_scholar'

    doi = db.Column(db.Text, primary_key=True)
    s2_url = db.Column(db.Text)
    s2_pdf_url = db.Column(db.Text)


class GreenScrapeAction(Enum):
    scrape_now = 1
    queue = 2
    none = 3


class Preprint(db.Model):
    preprint_id = db.Column(db.Text, primary_key=True)
    postprint_id = db.Column(db.Text, primary_key=True)

    def __repr__(self):
        return '<Preprint {}, {}>'.format(self.preprint_id, self.postprint_id)


class Retraction(db.Model):
    retraction_doi = db.Column(db.Text, primary_key=True)
    retracted_doi = db.Column(db.Text, primary_key=True)

    def __repr__(self):
        return '<Retraction {}, {}>'.format(self.retraction_doi,
                                            self.retracted_doi)


class FilteredPreprint(db.Model):
    preprint_id = db.Column(db.Text, primary_key=True)
    postprint_id = db.Column(db.Text, primary_key=True)

    def __repr__(self):
        return '<FilteredPreprint {}, {}>'.format(self.preprint_id,
                                                  self.postprint_id)


class PubRefreshResult(db.Model):
    id = db.Column(db.Text, primary_key=True)
    refresh_time = db.Column(db.DateTime, primary_key=True)
    oa_status_before = db.Column(db.Text)
    oa_status_after = db.Column(db.Text)

    def __repr__(self):
        return f'<PubRefreshResult({self.id}, {self.refresh_time}, {self.oa_status_before}, {self.oa_status_after})>'


class Pub(db.Model):
    id = db.Column(db.Text, primary_key=True)
    updated = db.Column(db.DateTime)
    crossref_api_raw_new = db.Column(JSONB)
    published_date = db.Column(db.DateTime)
    title = db.Column(db.Text)
    normalized_title = db.Column(db.Text)
    issns_jsonb = db.Column(JSONB)

    last_changed_date = db.Column(db.DateTime)
    response_jsonb = db.Column(JSONB)
    response_is_oa = db.Column(db.Boolean)
    response_best_evidence = db.Column(db.Text)
    response_best_url = db.Column(db.Text)
    response_best_host = db.Column(db.Text)
    response_best_repo_id = db.Column(db.Text)
    response_best_version = db.Column(db.Text)

    scrape_updated = db.Column(db.DateTime)
    scrape_evidence = db.Column(db.Text)
    scrape_pdf_url = db.Column(db.Text)
    scrape_metadata_url = db.Column(db.Text)
    scrape_license = db.Column(db.Text)

    resolved_doi_url = db.Column(db.Text)
    resolved_doi_http_status = db.Column(db.SmallInteger)
    doi_landing_page_is_archived = db.Column(db.Boolean)
    recordthresher_id = db.Column(db.Text)

    error = db.Column(db.Text)

    rand = db.Column(db.Numeric)

    pmcid_links = db.relationship(
        'PmcidLookup',
        lazy='subquery',
        viewonly=True,
        backref=db.backref("pub", lazy="subquery"),
        foreign_keys="PmcidLookup.doi"
    )

    page_matches_by_doi = db.relationship(
        'Page',
        lazy='subquery',
        viewonly=True,
        backref=db.backref("pub_by_doi", lazy="subquery"),
        foreign_keys="Page.doi"
    )

    repo_page_matches_by_doi = db.relationship(
        'RepoPage',
        lazy='subquery',
        viewonly=True,
        primaryjoin="and_(RepoPage.match_doi == True, RepoPage.doi == Pub.id)"
    )

    repo_page_matches_by_title = db.relationship(
        'RepoPage',
        lazy='subquery',
        viewonly=True,
        primaryjoin="and_(RepoPage.match_title == True, RepoPage.normalized_title == Pub.normalized_title)"
    )

    def __init__(self, **biblio):
        self.reset_vars()
        self.rand = random.random()
        self.license = None
        self.free_metadata_url = None
        self.free_pdf_url = None
        self.oa_status = None
        self.evidence = None
        self.open_locations = []
        self.embargoed_locations = []
        self.closed_urls = []
        self.session_id = None
        self.version = None
        self.issn_l = None
        self.openalex_journal_id = None
        # self.updated = datetime.datetime.utcnow()
        for (k, v) in biblio.items():
            self.__setattr__(k, v)

    @orm.reconstructor
    def init_on_load(self):
        self.reset_vars()

    def reset_vars(self):
        if self.id and self.id.startswith("10."):
            self.id = normalize_doi(self.id)

        self.license = None
        self.free_metadata_url = None
        self.free_pdf_url = None
        self.oa_status = None
        self.evidence = None
        self.open_locations = []
        self.embargoed_locations = []
        self.closed_urls = []
        self.session_id = None
        self.version = None

        issn_l_lookup = self.lookup_issn_l()
        self.issn_l = issn_l_lookup.issn_l if issn_l_lookup else None
        self.openalex_journal_id = issn_l_lookup.journal_id if issn_l_lookup else None

    @property
    def doi(self):
        return self.id

    @property
    def unpaywall_api_url(self):
        return "https://api.unpaywall.org/v2/{}?email=internal@impactstory.org".format(
            self.id)

    @property
    def tdm_api(self):
        return None

    @property
    def crossref_api_raw(self):
        record = None
        try:
            if self.crossref_api_raw_new:
                return self.crossref_api_raw_new
        except IndexError:
            pass

        return record

    @property
    def crossref_api_modified(self):
        record = None
        if self.crossref_api_raw_new:
            try:
                return build_crossref_record(self.crossref_api_raw_new)
            except IndexError:
                pass

        if self.crossref_api_raw:
            try:
                record = build_crossref_record(self.crossref_api_raw)
                print("got record")
                return record
            except IndexError:
                pass

        return record

    @property
    def open_urls(self):
        # return sorted urls, without dups
        urls = []
        for location in self.sorted_locations:
            if location.best_url not in urls:
                urls.append(location.best_url)
        return urls

    @property
    def url(self):
        if self.doi and self.doi.startswith('10.2218/forum.'):
            article_id = self.doi.split('.')[-1]
            return f'http://journals.ed.ac.uk/forum/article/view/{article_id}'

        return "https://doi.org/{}".format(self.id)

    @property
    def is_oa(self):
        return bool(self.fulltext_url)

    @property
    def is_paratext(self):
        paratext_exprs = [
            r'^Author Guidelines$',
            r'^Author Index$'
            r'^Back Cover',
            r'^Back Matter',
            r'^Contents$',
            r'^Contents:',
            r'^Cover Image',
            r'^Cover Picture',
            r'^Editorial Board',
            r'Editor Report$',
            r'^Front Cover',
            r'^Frontispiece',
            r'^Graphical Contents List$',
            r'^Index$',
            r'^Inside Back Cover',
            r'^Inside Cover',
            r'^Inside Front Cover',
            r'^Issue Information',
            r'^List of contents',
            r'^List of Tables$',
            r'^List of Figures$',
            r'^List of Plates$',
            r'^Masthead',
            r'^Pages de début$',
            r'^Title page',
            r"^Editor's Preface",
        ]

        for expr in paratext_exprs:
            if self.title and re.search(expr, self.title, re.IGNORECASE):
                return True

        return False

    @property
    def is_retracted(self):
        return bool(
            Retraction.query.filter(Retraction.retracted_doi == self.doi).all()
        )

    def recalculate(self, quiet=False, ask_preprint=True):
        self.clear_locations()

        if self.publisher == "CrossRef Test Account":
            self.error += "CrossRef Test Account"
            raise NoDoiException

        if self.journal == "CrossRef Listing of Deleted DOIs":
            self.error += "CrossRef Deleted DOI"
            raise NoDoiException

        self.find_open_locations(ask_preprint)
        self.decide_if_open()
        self.set_license_hacks()

        if self.is_oa and not quiet:
            logger.info(
                "**REFRESH found a fulltext_url for {}!  {}: {} **".format(
                    self.id, self.oa_status.value, self.fulltext_url))

    def refresh_crossref(self):
        from put_crossref_in_db import get_api_for_one_doi
        self.crossref_api_raw_new = get_api_for_one_doi(self.doi)

    def refresh_including_crossref(self):
        self.refresh_crossref()
        return self.refresh()

    def do_not_refresh(self):
        current_oa_status = self.response_jsonb and self.response_jsonb.get(
            'oa_status', None)
        if current_oa_status and current_oa_status == "gold" or current_oa_status == "hybrid":
            issns_to_refresh = [
                "2152-7180",
                "1687-8507",
                "1131-5598",
                "2083-2931",
                "2008-322X",
                "2152-7180, 2152-7199, 0037-8046, 1545-6846, 0024-3949, 1613-396X, 1741-2862, 0047-1178",
                "2598-0025"
            ]
            r = requests.get(
                f"https://parseland.herokuapp.com/parse-publisher?doi={self.id}")
            if r.status_code != 200:
                logger.info(
                    f"need to refresh gold or hybrid because parseland is bad response {self.id}")
                return False
            elif self.issn_l in issns_to_refresh:
                logger.info(
                    f"need to refresh gold or hybrid because of the journal {self.id}")
                return False
            elif self.scrape_license and self.scrape_license == 'mit':
                return False
            return True

    def refresh(self, session_id=None, force=False):
        if self.do_not_refresh() and self.resolved_doi_http_status is not None and not force:
            logger.info(
                f"not refreshing {self.id} because it's already gold or hybrid. Updating record thresher.")
            self.store_or_remove_pdf_urls_for_validation()
            self.store_refresh_priority()
            self.create_or_update_recordthresher_record()
            db.session.merge(self)
            return

        self.session_id = session_id or get_session_id()
        refresh_result = PubRefreshResult(
            id=self.id,
            refresh_time=datetime.datetime.utcnow(),
            oa_status_before=self.response_jsonb and self.response_jsonb.get(
                'oa_status', None)
        )

        if self.is_closed_exception():
            logger.info(f'{self.doi} is closed exception. Setting to closed and skipping hybrid refresh')
            self.open_locations = []
        else:
            self.refresh_hybrid_scrape()

        # and then recalculate everything, so can do to_dict() after this and it all works
        self.update()

        refresh_result.oa_status_after = self.response_jsonb and self.response_jsonb.get(
            'oa_status', None)
        db.session.merge(refresh_result)

        # then do this so the recalculated stuff saves
        # it's ok if this takes a long time... is a short time compared to refresh_hybrid_scrape
        self.create_or_update_recordthresher_record()
        db.session.merge(self)

    def create_or_update_parseland_record(self):
        if pl_record := ParselandRecordMaker.make_record(self):
            db.session.merge(pl_record)

    def create_or_update_recordthresher_record(self, all_records=True):
        if self.error and "crossref deleted doi" in self.error.lower():
            return False
        if rt_record := CrossrefRecordMaker.make_record(self):
            db.session.merge(rt_record)
            if not all_records:
                return True
            self.recordthresher_id = rt_record.id
            secondary_records = PmhRecordMaker.make_secondary_repository_responses(
                rt_record)
            for secondary_record in secondary_records:
                db.session.merge(secondary_record)
                db.session.merge(
                    RecordthresherParentRecord(record_id=secondary_record.id,
                                               parent_record_id=rt_record.id))
            self.create_or_update_parseland_record()
            if self.is_oa and (pdf_record := PDFRecordMaker.make_record(self)):
                db.session.merge(pdf_record)
            return True
        return False

    def set_results(self):
        self.issns_jsonb = self.issns
        self.response_jsonb = self.to_dict_v2()
        self.response_is_oa = self.is_oa
        self.response_best_url = self.best_url
        self.response_best_evidence = self.best_evidence
        self.response_best_version = self.best_version
        self.response_best_host = self.best_host
        self.response_best_repo_id = self.best_repo_id

    def clear_results(self):
        self.response_jsonb = None
        self.response_is_oa = None
        self.response_best_url = None
        self.response_best_evidence = None
        self.response_best_version = None
        self.response_best_host = None
        self.response_best_repo_id = None
        self.error = ""
        self.issns_jsonb = None

    @staticmethod
    def ignored_keys_for_internal_diff():
        # remove these keys from comparison because their contents are volatile or we don't care about them
        return ["updated", "last_changed_date",
                "x_reported_noncompliant_copies", "x_error", "data_standard"]

    @staticmethod
    def ignored_keys_for_external_diff():
        # remove these keys because they have been added to the api response but we don't want to trigger a diff
        return Pub.ignored_keys_for_internal_diff()

    @staticmethod
    def ignored_top_level_keys_for_external_diff():
        # existing ignored key regex method doesn't work for multiline keys
        # but don't want to replace it yet because it works on nested rows
        return ["z_authors", "oa_locations_embargoed"]

    @staticmethod
    def remove_response_keys(jsonb_response, keys):
        response_copy = json.loads(json.dumps(jsonb_response))

        for key in keys:
            try:
                del response_copy[key]
            except KeyError:
                pass

        return response_copy

    def has_changed(self, old_response_jsonb, ignored_keys,
                    ignored_top_level_keys):
        if not old_response_jsonb:
            logger.info(
                "response for {} has changed: no old response".format(self.id))
            return True

        copy_of_new_response = Pub.remove_response_keys(self.response_jsonb,
                                                        ignored_top_level_keys)
        copy_of_old_response = Pub.remove_response_keys(old_response_jsonb,
                                                        ignored_top_level_keys)

        # have to sort to compare
        copy_of_new_response_in_json = json.dumps(copy_of_new_response,
                                                  sort_keys=True, indent=2)
        # have to sort to compare
        copy_of_old_response_in_json = json.dumps(copy_of_old_response,
                                                  sort_keys=True, indent=2)

        for key in ignored_keys:
            # remove it
            copy_of_new_response_in_json = re.sub(
                r'"{}":\s*".+?",?\s*'.format(key), '',
                copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(
                r'"{}":\s*".+?",?\s*'.format(key), '',
                copy_of_old_response_in_json)

            # also remove it if it is an empty list
            copy_of_new_response_in_json = re.sub(
                r'"{}":\s*\[\],?\s*'.format(key), '',
                copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(
                r'"{}":\s*\[\],?\s*'.format(key), '',
                copy_of_old_response_in_json)

            # also anything till a comma (gets data_standard)
            copy_of_new_response_in_json = re.sub(
                r'"{}":\s*.+?,\s*'.format(key), '',
                copy_of_new_response_in_json)
            copy_of_old_response_in_json = re.sub(
                r'"{}":\s*.+?,\s*'.format(key), '',
                copy_of_old_response_in_json)

        return copy_of_new_response_in_json != copy_of_old_response_in_json

    def update(self):
        return self.recalculate_and_store()

    def recalculate_and_store(self):
        if not self.crossref_api_raw_new:
            self.crossref_api_raw_new = self.crossref_api_raw

        self.title = self.crossref_title
        self.normalized_title = normalize_title(self.title)
        if not self.published_date:
            self.published_date = self.issued
        if not self.rand:
            self.rand = random.random()

        old_response_jsonb = self.response_jsonb

        self.clear_results()
        try:
            self.recalculate()
        except NoDoiException:
            logger.info("invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass

        self.set_results()
        self.mint_pages()
        self.scrape_green_locations(GreenScrapeAction.queue)
        self.store_or_remove_pdf_urls_for_validation()
        self.store_refresh_priority()
        self.store_preprint_relationships()
        self.store_retractions()
        response_changed = self.decide_if_response_changed(old_response_jsonb)

    def decide_if_response_changed(self, old_response_jsonb):
        response_changed = False

        if self.has_changed(old_response_jsonb,
                            Pub.ignored_keys_for_external_diff(),
                            Pub.ignored_top_level_keys_for_external_diff()):
            logger.info(
                "changed! updating last_changed_date for this record! {}".format(
                    self.id))
            self.last_changed_date = datetime.datetime.utcnow().isoformat()
            response_changed = True

        if self.has_changed(old_response_jsonb,
                            Pub.ignored_keys_for_internal_diff(), []):
            logger.info(
                "changed! updating updated timestamp for this record! {}".format(
                    self.id))
            self.updated = datetime.datetime.utcnow()
            self.response_jsonb[
                'updated'] = datetime.datetime.utcnow().isoformat()
            response_changed = True

        if response_changed:
            flag_modified(self, "response_jsonb")  # force it to be saved
        else:
            self.response_jsonb = old_response_jsonb  # don't save if only ignored fields changed

        return response_changed

    def run(self):
        try:
            self.recalculate_and_store()
        except NoDoiException:
            logger.info("invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass
        # logger.info(json.dumps(self.response_jsonb, indent=4))

    def run_with_hybrid(self, quiet=False, shortcut_data=None):
        logger.info("in run_with_hybrid")
        self.clear_results()
        try:
            self.refresh()
        except NoDoiException:
            logger.info("invalid doi {}".format(self))
            self.error += "Invalid DOI"
            pass

        # set whether changed or not
        self.set_results()

    @property
    def has_been_run(self):
        if self.evidence:
            return True
        return False

    @property
    def best_redirect_url(self):
        return self.fulltext_url or self.url

    @property
    def has_fulltext_url(self):
        return self.fulltext_url is not None

    @property
    def has_license(self):
        if not self.license:
            return False
        if self.license == "unknown":
            return False
        return True

    @property
    def clean_doi(self):
        if not self.id:
            return None
        return normalize_doi(self.id)

    def ask_manual_overrides(self):
        if not self.doi:
            return

        override_dict = oa_manual.get_override_dict(self)
        if override_dict is not None:
            logger.info("manual override for {}".format(self.doi))
            self.open_locations = []
            if override_dict:
                my_location = OpenLocation()
                my_location.pdf_url = None
                my_location.metadata_url = None
                my_location.license = None
                my_location.version = None
                my_location.evidence = "manual"
                my_location.doi = self.doi

                # set just what the override dict specifies
                for (k, v) in override_dict.items():
                    setattr(my_location, k, v)

                # don't append, make it the only one
                self.open_locations.append(my_location)

            self.updated = datetime.datetime.utcnow()
            self.last_changed_date = datetime.datetime.utcnow()

    def ask_preprints(self):
        preprint_relationships = FilteredPreprint.query.filter(
            FilteredPreprint.postprint_id == self.doi).all()
        for preprint_relationship in preprint_relationships:
            preprint_pub = Pub.query.get(preprint_relationship.preprint_id)
            if preprint_pub:
                try:
                    # don't look for pre/postprints here or you get circular lookups
                    preprint_pub.recalculate(ask_preprint=False)
                    # get the best location that's actually a preprint - don't include other copies of the preprint
                    all_locations = preprint_pub.deduped_sorted_locations
                    preprint_locations = [loc for loc in all_locations if
                                          loc.host_type == 'repository']
                    if preprint_locations:
                        self.open_locations.append(preprint_locations[0])
                except NoDoiException:
                    pass

    def ask_postprints(self):
        preprint_relationships = FilteredPreprint.query.filter(
            FilteredPreprint.preprint_id == self.doi).all()
        for preprint_relationship in preprint_relationships:
            postprint_pub = Pub.query.get(preprint_relationship.postprint_id)
            if postprint_pub:
                try:
                    # don't look for pre/postprints here or you get circular lookups
                    postprint_pub.recalculate(ask_preprint=False)
                    # get the best location that's actually a postprint - don't include other preprints
                    all_locations = postprint_pub.deduped_sorted_locations
                    postprint_locations = [loc for loc in all_locations if
                                           loc.host_type == 'publisher' and loc.version == 'publishedVersion']
                    if postprint_locations:
                        self.open_locations.append(postprint_locations[0])
                except NoDoiException:
                    pass

    @property
    def fulltext_url(self):
        return self.free_pdf_url or self.free_metadata_url or None

    @property
    def is_preprint(self):
        return self.genre == 'posted-content' and not self.issns

    def make_preprint(self, oa_location):
        oa_location.evidence = re.sub(r'.*?(?= \(|$)', 'oa repository',
                                      oa_location.evidence or '', 1)
        oa_location.version = "submittedVersion"

    def decide_if_open(self):
        # look through the locations here

        # overwrites, hence the sorting
        self.license = None
        self.free_metadata_url = None
        self.free_pdf_url = None
        self.oa_status = OAStatus.closed
        self.version = None
        self.evidence = None

        reversed_sorted_locations = self.sorted_locations
        reversed_sorted_locations.reverse()

        # go through all the locations, using valid ones to update the best open url data
        for location in reversed_sorted_locations:
            self.free_pdf_url = location.pdf_url
            self.free_metadata_url = location.metadata_url
            self.evidence = location.evidence
            self.version = location.version
            self.license = location.license

        if reversed_sorted_locations:
            if self.is_preprint:
                self.oa_status = OAStatus.green
            else:
                self.oa_status = \
                    sorted(reversed_sorted_locations, key=oa_status_sort_key)[
                        -1].oa_status

        # don't return an open license on a closed thing, that's confusing
        if not self.fulltext_url:
            self.license = None
            self.evidence = None
            self.oa_status = OAStatus.closed
            self.version = None

    def clear_locations(self):
        self.reset_vars()

    @property
    def has_hybrid(self):
        return any([location.oa_status is OAStatus.hybrid for location in
                    self.all_oa_locations])

    @property
    def has_gold(self):
        return any([location.oa_status is OAStatus.gold for location in
                    self.all_oa_locations])

    @property
    def has_green(self):
        return any([location.oa_status is OAStatus.green for location in
                    self.all_oa_locations])

    def refresh_green_locations(self):
        for my_page in self.pages:
            my_page.scrape()

    def refresh_hybrid_scrape(self):
        logger.info("***** {}: {}".format(self.publisher, self.journal))
        # look for hybrid
        self.scrape_updated = datetime.datetime.utcnow()

        # reset
        self.scrape_evidence = None
        self.scrape_pdf_url = None
        self.scrape_metadata_url = None
        self.scrape_license = None
        self.resolved_doi_url = None

        if self.url:
            with PublisherWebpage(url=self.url,
                                  related_pub_doi=self.doi,
                                  related_pub_publisher=self.publisher,
                                  session_id=self.session_id,
                                  issn_l=self.issn_l) as publisher_landing_page:

                # end the session before the scrape
                # logger.info(u"closing session for {}".format(self.doi))
                db.session.close()

                self.scrape_page_for_open_location(publisher_landing_page)
                self.resolved_doi_url = publisher_landing_page.resolved_url
                self.resolved_doi_http_status = publisher_landing_page.resolved_http_status_code

                # now merge our object back in
                # logger.info(u"after scrape, merging {}".format(self.doi))
                db.session.merge(self)

                self.save_landing_page_text(publisher_landing_page.page_text)
                save_pdf(self.doi, publisher_landing_page.pdf_content)

                if publisher_landing_page.is_open:
                    self.scrape_evidence = publisher_landing_page.open_version_source_string
                    self.scrape_pdf_url = publisher_landing_page.scraped_pdf_url
                    self.scrape_metadata_url = publisher_landing_page.scraped_open_metadata_url
                    self.scrape_license = publisher_landing_page.scraped_license
                    if (publisher_landing_page.is_open
                            and not publisher_landing_page.scraped_pdf_url
                            and not publisher_landing_page.use_resolved_landing_url(
                                publisher_landing_page.scraped_open_metadata_url)
                    ):
                        self.scrape_metadata_url = self.url

                # Academic Medicine, delayed OA
                if self.issn_l == '1040-2446' and self.issued < datetime.datetime.utcnow().date() - relativedelta(
                        months=14):
                    if not self.scrape_metadata_url:
                        self.scrape_evidence = 'open (via free article)'
                        self.scrape_metadata_url = publisher_landing_page.resolved_url
                        logger.info(
                            'making {} bronze due to delayed OA policy'.format(
                                self.doi))

                # Genome Research, delayed OA
                if self.issn_l == '1088-9051' and (
                        self.issued < datetime.datetime.utcnow().date() - relativedelta(
                    months=7) or self.scrape_pdf_url):
                    logger.info(
                        'making {} hybrid due to delayed OA policy'.format(
                            self.doi))
                    self.scrape_evidence = 'open (via page says license)'
                    self.scrape_metadata_url = self.url
                    self.scrape_license = 'cc-by-nc'

        return

    def save_landing_page_text(self, page_text):
        if not page_text:
            return

        try:
            logger.info(
                f'saving {len(page_text)} characters to {self.landing_page_archive_url()}')
            s3.put_object(
                Body=gzip.compress(page_text.encode('utf-8')),
                Bucket=LANDING_PAGE_ARCHIVE_BUCKET,
                Key=self.landing_page_archive_key()
            )
            self.doi_landing_page_is_archived = True
        except Exception as e:
            # page text is just nice-to-have for now
            logger.error(f'failed to save landing page: {e}')

    def find_open_locations(self, ask_preprint=True):
        # just based on doi
        if self.is_closed_exception():
            self.open_locations = []
            return

        if local_lookup := self.ask_local_lookup():
            if local_lookup['is_future']:
                self.embargoed_locations.append(local_lookup['location'])
            else:
                self.open_locations.append(local_lookup['location'])

        self.ask_pmc()

        # based on titles
        self.set_title_hacks()  # has to be before ask_green_locations, because changes titles

        self.ask_green_locations()
        self.ask_publisher_equivalent_pages()
        self.ask_hybrid_scrape()
        self.ask_s2()

        if ask_preprint:
            self.ask_preprints()
            self.ask_postprints()

        self.ask_manual_overrides()
        self.remove_redundant_embargoed_locations()

    def landing_page_archive_key(self):
        return urllib.parse.quote(self.doi, safe='')

    def landing_page_archive_url(self):
        return f's3://{LANDING_PAGE_ARCHIVE_BUCKET}/{self.landing_page_archive_key()}'

    def pdf_archive_key(self):
        """The key is the DOI with the suffix _publishedVersion.pdf"""
        return f"{urllib.parse.quote(self.doi, safe='')}.pdf"

    def pdf_archive_url(self):
        return f's3://{PDF_ARCHIVE_BUCKET}/{self.pdf_archive_key()}'

    def remove_redundant_embargoed_locations(self):
        if any([loc.host_type == 'publisher' for loc in self.all_oa_locations]):
            self.embargoed_locations = [loc for loc in self.embargoed_locations
                                        if loc.host_type != 'publisher']

    def is_springer_ebook(self):
        return all([self.doi.startswith('10.1007'),
                    'springer' in (self.publisher.lower() or ''),
                    'book' in (self.genre or '')])

    def is_closed_exception(self):
        return any([self.is_springer_ebook(),
                    '1751-2409' in self.issns,
                    '1751-2395' in self.issns])

    def ask_local_lookup(self):
        evidence = None
        fulltext_url = self.url

        license = None
        pdf_url = None
        version = "publishedVersion"  # default
        oa_date = None
        publisher_specific_license = None

        if oa_local.is_open_via_doaj(self.issns, self.all_journals, self.year):
            license = oa_local.is_open_via_doaj(self.issns, self.all_journals,
                                                self.year)
            evidence = oa_evidence.oa_journal_doaj
            oa_date = self.issued
            crossref_license = oa_local.is_open_via_license_urls(
                self.crossref_licenses, self.issns)
            if crossref_license:
                freetext_license = crossref_license['url']
                license = oa_local.find_normalized_license(freetext_license)
            elif (
                    any(self.is_same_publisher(p) for p in
                        ['BMJ', 'Swiss Chemical Society'])
                    and self.scrape_license
            ):
                license = self.scrape_license
        elif oa_local.is_open_via_publisher(self.publisher):
            evidence = oa_evidence.oa_journal_publisher
            license = oa_local.find_normalized_license(
                oa_local.is_open_via_publisher(self.publisher))
            if license == 'unspecified-oa' and self.scrape_license:
                license = self.scrape_license
            oa_date = self.issued
        elif oa_local.is_open_via_publisher_genre(self.publisher, self.genre):
            evidence = oa_evidence.oa_journal_publisher
            license = oa_local.find_normalized_license(
                oa_local.is_open_via_publisher_genre(self.publisher,
                                                     self.genre))
            oa_date = self.issued
        elif self.is_open_journal_via_observed_oa_rate():
            evidence = oa_evidence.oa_journal_observed
            oa_date = self.issued
        elif oa_local.is_open_via_manual_journal_setting(self.issns, self.year):
            evidence = oa_evidence.oa_journal_manual
            oa_date = self.issued
            license = oa_local.manual_gold_journal_license(self.issn_l)
        elif oa_local.is_open_via_doi_fragment(self.doi):
            evidence = "oa repository (via doi prefix)"
            oa_date = self.issued
        elif oa_local.is_open_via_journal_doi_prefix(self.doi):
            evidence = "oa journal (via doi prefix)"
            oa_date = self.issued
        elif oa_local.is_open_via_url_fragment(self.url):
            evidence = "oa repository (via url prefix)"
            oa_date = self.issued
        elif oa_local.is_open_via_license_urls(self.crossref_licenses,
                                               self.issns):
            crossref_license = oa_local.is_open_via_license_urls(
                self.crossref_licenses, self.issns)
            freetext_license = crossref_license['url']
            license = oa_local.find_normalized_license(freetext_license)
            evidence = "open (via crossref license)"
            oa_date = crossref_license['date'] or self.issued
        elif self.open_manuscript_licenses:
            manuscript_license = self.open_manuscript_licenses[-1]
            has_open_manuscript = True
            freetext_license = manuscript_license['url']
            license = oa_local.find_normalized_license(freetext_license)
            oa_date = manuscript_license['date'] or self.issued
            if freetext_license and not license:
                license = "publisher-specific-oa"
                publisher_specific_license = freetext_license
            version = "acceptedVersion"
            if self.is_same_publisher("Elsevier BV"):
                elsevier_id = self.crossref_alternative_id
                pdf_url = "http://manuscript.elsevier.com/{}/pdf/{}.pdf".format(
                    elsevier_id, elsevier_id)
            elif self.is_same_publisher("American Physical Society (APS)"):
                proper_case_id = self.id
                proper_case_id = proper_case_id.replace("revmodphys",
                                                        "RevModPhys")
                proper_case_id = proper_case_id.replace("physrevlett",
                                                        "PhysRevLett")
                proper_case_id = proper_case_id.replace("physreva", "PhysRevA")
                proper_case_id = proper_case_id.replace("physrevb", "PhysRevB")
                proper_case_id = proper_case_id.replace("physrevc", "PhysRevC")
                proper_case_id = proper_case_id.replace("physrevd", "PhysRevD")
                proper_case_id = proper_case_id.replace("physreve", "PhysRevE")
                proper_case_id = proper_case_id.replace("physrevx", "PhysRevX")
                proper_case_id = proper_case_id.replace("physrevaccelbeams",
                                                        "PhysRevAccelBeams")
                proper_case_id = proper_case_id.replace("physrevapplied",
                                                        "PhysRevApplied")
                proper_case_id = proper_case_id.replace("physrevphyseducres",
                                                        "PhysRevPhysEducRes")
                proper_case_id = proper_case_id.replace("physrevstper",
                                                        "PhysRevSTPER")
                if proper_case_id != self.id:
                    pdf_url = "https://link.aps.org/accepted/{}".format(
                        proper_case_id)
            elif self.is_same_publisher("AIP Publishing"):
                pdf_url = "https://aip.scitation.org/doi/{}".format(self.id)
            elif self.is_same_publisher("IOP Publishing"):
                has_open_manuscript = False
            elif self.is_same_publisher("Wiley-Blackwell"):
                has_open_manuscript = False
            elif self.is_same_publisher("Wiley"):
                pdf_url = 'https://rss.onlinelibrary.wiley.com/doi/am-pdf/{}'.format(
                    self.doi)
            elif self.is_same_publisher("American Geophysical Union (AGU)"):
                pdf_url = 'https://rss.onlinelibrary.wiley.com/doi/am-pdf/{}'.format(
                    self.doi)
            elif self.is_same_publisher("Royal Society of Chemistry (RSC)"):
                has_open_manuscript = False
            elif self.is_same_publisher("Oxford University Press (OUP)"):
                has_open_manuscript = False
                # just bail for now. is too hard to figure out which ones are real.

                # # IOP isn't trustworthy, and made a fuss, so check them.
                # # this includes /ampdf: http://iopscience.iop.org/article/10.1088/0029-5515/55/8/083011
                # # this does not: http://iopscience.iop.org/article/10.1088/1741-2552/aad46e
                #
                # logger.info(u"doing live check on IOP author manuscript")
                # r = requests.get("http://iopscience.iop.org/article/{}".format(self.id))
                # if "/ampdf" in r.content:
                #     logger.info(u"is iop open manuscript!")
                #     pdf_url = "http://iopscience.iop.org/article/{}/ampdf".format(self.id)
                # else:
                #     logger.info(u"is NOT iop open manuscript")
                #     has_open_manuscript = False
            elif freetext_license == 'https://academic.oup.com/journals/pages/open_access/funder_policies/chorus/standard_publication_model':
                # license says available after 12 months
                oa_date = self.issued + relativedelta(months=12)

            if has_open_manuscript:
                evidence = "open (via crossref license, author manuscript)"
        elif self.predicted_bronze_embargo_end:
            evidence = "embargoed (via journal policy)"
            oa_date = self.predicted_bronze_embargo_end

        if (
                evidence
                and self.resolved_doi_url
                and self.resolved_doi_url.startswith('https://journals.co.za')
                and self.resolved_doi_http_status == 404
        ):
            fulltext_url = 'https://journals.co.za/doi/{}'.format(
                self.id.upper())
            self.resolved_doi_http_status = 203

        failed_scrape = self.resolved_doi_http_status in [404,
                                                          -1] and self.issn_l not in [
                            '2324-1098',
                            # gold and online, but can't scrape it for some reason
                        ]

        if evidence and not failed_scrape:
            my_location = OpenLocation()
            my_location.metadata_url = fulltext_url
            my_location.license = license
            my_location.evidence = evidence
            my_location.updated = datetime.datetime.utcnow()
            my_location.doi = self.doi
            my_location.version = version
            my_location.oa_date = oa_date
            my_location.not_ol_exception_func = self.elsevier_bronze_exception_func(
                license)
            my_location.publisher_specific_license = publisher_specific_license
            if pdf_url:
                my_location.pdf_url = pdf_url

            is_future = my_location.oa_date and my_location.oa_date > datetime.datetime.utcnow().date()

            if my_location.oa_status is OAStatus.bronze and not is_future:
                my_location.oa_date = None

            if self.is_preprint:
                self.make_preprint(my_location)

            return {'location': my_location, 'is_future': is_future}

        return None

    def ask_pmc(self):
        for pmc_obj in self.pmcid_links:
            if pmc_obj.release_date == "live":
                my_location = OpenLocation()
                my_location.metadata_url = "https://www.ncbi.nlm.nih.gov/pmc/articles/{}".format(
                    pmc_obj.pmcid.upper())
                # we don't know this has a pdf version
                # my_location.pdf_url = "https://www.ncbi.nlm.nih.gov/pmc/articles/{}/pdf".format(pmc_obj.pmcid.upper())
                my_location.evidence = "oa repository (via pmcid lookup)"
                my_location.updated = datetime.datetime.utcnow()
                my_location.doi = self.doi
                my_location.version = pmc_obj.version
                # set version in one central place for pmc right now, till refactor done
                self.open_locations.append(my_location)

    @property
    def has_stored_hybrid_scrape(self):
        return self.scrape_evidence and self.scrape_evidence != "closed"

    def ask_hybrid_scrape(self):
        return_location = None

        if self.has_stored_hybrid_scrape:
            my_location = OpenLocation()
            my_location.pdf_url = self.scrape_pdf_url
            my_location.metadata_url = self.scrape_metadata_url
            my_location.license = self.scrape_license
            my_location.evidence = self.scrape_evidence
            my_location.not_ol_exception_func = self.elsevier_bronze_exception_func(
                self.scrape_license)
            my_location.updated = self.scrape_updated and self.scrape_updated.isoformat()
            my_location.doi = self.doi
            my_location.version = "publishedVersion"

            if my_location.pdf_url and '/article/am/pii/' in my_location.pdf_url:
                my_location.version = "acceptedVersion"

            if self.is_preprint:
                self.make_preprint(my_location)

            if my_location.oa_status in [OAStatus.gold, OAStatus.hybrid,
                                         OAStatus.green]:
                my_location.oa_date = self.issued

            if self.issn_l == '0270-6474' and my_location.oa_date:
                my_location.oa_date = my_location.oa_date + datetime.timedelta(
                    days=190)
                if my_location.oa_date and my_location.oa_date > datetime.datetime.utcnow().date():
                    self.embargoed_locations.append(my_location)
                else:
                    self.open_locations.append(my_location)
                    return_location = my_location
            else:
                self.open_locations.append(my_location)
                return_location = my_location

        return return_location

    @property
    def page_matches_by_doi_filtered(self):
        return self.page_matches_by_doi + self.repo_page_matches_by_doi

    @property
    def page_matches_by_title_filtered(self):

        my_pages = []

        if not self.normalized_title:
            return my_pages

        for my_page in self.repo_page_matches_by_title:
            # don't do this right now.  not sure if it helps or hurts.
            # don't check title match if we already know it belongs to a different doi
            # if my_page.doi and my_page.doi != self.doi:
            #     continue

            if hasattr(my_page,
                       "pmh_record") and my_page.pmh_record and is_known_mismatch(
                self.id, my_page.pmh_record):
                continue

            # double check author match
            match_type = "title"
            if self.first_author_lastname or self.last_author_lastname:
                if my_page.authors:
                    try:
                        pmh_author_string = normalize(
                            ", ".join(my_page.authors))
                        if self.first_author_lastname and normalize(
                                self.first_author_lastname) in pmh_author_string:
                            match_type = "title and first author"
                        elif self.last_author_lastname and normalize(
                                self.last_author_lastname) in pmh_author_string:
                            match_type = "title and last author"
                        else:
                            # logger.info(
                            #    u"author check fails, so skipping this record. Looked for {} and {} in {}".format(
                            #       self.first_author_lastname, self.last_author_lastname, pmh_author_string))
                            # logger.info(self.authors)
                            # don't match if bad author match
                            continue
                    except TypeError:
                        pass  # couldn't make author string
            my_page.match_evidence = "oa repository (via OAI-PMH {} match)".format(
                match_type)
            my_pages.append(my_page)
        return my_pages

    def page_new(self, filter_f=lambda x: True):
        if p_new := [p for p in self.pages if
                     isinstance(p, page.PageNew) and filter_f(p)]:
            return p_new[0]
        return None

    @property
    def pages(self):
        my_pages = []

        # @todo remove these checks once we are just using the new page
        if self.normalized_title:
            if title_is_too_short(self.normalized_title):
                # logger.info(u"title too short! don't match by title")
                pass
            elif title_is_too_common(self.normalized_title):
                # logger.info(u"title too common!  don't match by title.")
                pass
            elif self.id and '/(issn)' in self.id.lower():
                pass
            else:
                my_pages = self.page_matches_by_title_filtered

        if max_pages_from_one_repo([p.endpoint_id for p in
                                    self.page_matches_by_title_filtered]) >= 10:
            my_pages = []
            logger.info(
                "matched too many pages in one repo, not allowing matches")

        # do dois last, because the objects are actually the same, not copies, and then they get the doi reason
        for my_page in self.page_matches_by_doi_filtered:
            my_page.match_evidence = "oa repository (via OAI-PMH doi match)"
            if not my_page.scrape_version and "/pmc/" in my_page.url:
                my_page.set_info_for_pmc_page()

            my_pages.append(my_page)

        return [
            p for p in my_pages
            # don't match bioRxiv or Research Square preprints to themselves
            if not (p.doi == self.doi and p.endpoint_id in [
                oa_page.biorxiv_endpoint_id, oa_page.research_square_endpoint_id
            ])
        ]

    def ask_green_locations(self):
        has_new_green_locations = False
        springer_ebook_oa_override = any([(
                                          page.scrape_pdf_url and page.scrape_pdf_url.endswith(
                                              '?pdf=chapter%20toc') for page in
                                          self.pages)]) and self.publisher and 'springer' in self.publisher.lower()
        if springer_ebook_oa_override:
            return
        for my_page in [p for p in self.pages if
                        p.pmh_id != oa_page.publisher_equivalent_pmh_id]:
            # this step isn't scraping, is just looking in db
            # recalculate the version and license based on local PMH metadata in case code changes find more things
            if hasattr(my_page,
                       "scrape_version") and my_page.scrape_version is not None:
                my_page.update_with_local_info()

            if my_page.is_open:
                new_open_location = OpenLocation()
                new_open_location.pdf_url = my_page.scrape_pdf_url
                new_open_location.metadata_url = my_page.scrape_metadata_url
                new_open_location.license = my_page.scrape_license
                new_open_location.evidence = my_page.match_evidence
                new_open_location.version = my_page.scrape_version
                new_open_location.updated = my_page.scrape_updated
                new_open_location.doi = self.doi
                new_open_location.pmh_id = my_page.bare_pmh_id
                new_open_location.not_ol_exception_func = self.elsevier_bronze_exception_func(
                    my_page.scrape_license)
                new_open_location.endpoint_id = my_page.endpoint_id
                new_open_location.institution = my_page.repository_display_name
                new_open_location.oa_date = my_page.first_available

                # dates only reliably recorded after 2020-08-07
                if new_open_location.oa_date and new_open_location.oa_date < datetime.date(
                        2020, 8, 7):
                    new_open_location.oa_date = None

                self.open_locations.append(new_open_location)
                has_new_green_locations = True
        return has_new_green_locations

    def ask_publisher_equivalent_pages(self):
        has_new_green_locations = False
        for my_page in [p for p in self.pages if
                        p.pmh_id == oa_page.publisher_equivalent_pmh_id]:
            if my_page.is_open:
                new_open_location = OpenLocation()
                new_open_location.pdf_url = my_page.scrape_pdf_url
                new_open_location.metadata_url = my_page.scrape_metadata_url
                new_open_location.license = my_page.scrape_license
                new_open_location.evidence = my_page.scrape_version
                new_open_location.version = 'publishedVersion'
                new_open_location.updated = my_page.scrape_updated
                new_open_location.not_ol_exception_func = self.elsevier_bronze_exception_func(
                    my_page.scrape_license)
                new_open_location.doi = my_page.doi
                new_open_location.pmh_id = None
                new_open_location.endpoint_id = None

                if new_open_location.is_hybrid:
                    new_open_location.oa_date = self.issued

                self.open_locations.append(new_open_location)
                has_new_green_locations = True
        return has_new_green_locations

    def ask_s2(self):
        lookup = db.session.query(S2Lookup).get(self.doi)
        if lookup:
            location = OpenLocation()
            location.endpoint_id = s2_endpoint_id
            location.pdf_url = lookup.s2_pdf_url
            location.metadata_url = lookup.s2_url
            location.evidence = 'oa repository (semantic scholar lookup)'
            location.updated = datetime.datetime(2019, 10, 1)
            location.doi = self.doi
            location.version = 'submittedVersion'
            self.open_locations.append(location)

    def scrape_green_locations(self, green_scrape=GreenScrapeAction.queue):
        for my_page in self.pages:
            if isinstance(my_page, page.PageNew):
                if green_scrape is GreenScrapeAction.scrape_now:
                    my_page.scrape_if_matches_pub()
                elif green_scrape is GreenScrapeAction.queue:
                    my_page.enqueue_scrape_if_matches_pub()

    # comment out for now so that not scraping by accident
    # def scrape_these_pages(self, webpages):
    #     webpage_arg_list = [[page] for page in webpages]
    #     call_args_in_parallel(self.scrape_page_for_open_location, webpage_arg_list)

    def scrape_page_for_open_location(self, my_webpage):
        try:
            if not self.should_scrape_publisher_page():
                logger.info('skipping publisher scrape')
                return

            find_pdf_link = self.should_look_for_publisher_pdf()

            if not find_pdf_link:
                logger.info('skipping pdf search')

            my_webpage.scrape_for_fulltext_link(find_pdf_link=find_pdf_link,
                                                pdf_hint=self.crossref_text_mining_pdf)
            if self.error is None:
                self.error = ''

            if my_webpage.error:
                self.error += my_webpage.error

            if my_webpage.is_open:
                my_open_location = my_webpage.mint_open_location()
                self.open_locations.append(my_open_location)
                # logger.info(u"found open version at", webpage.url)
            else:
                # logger.info(u"didn't find open version at", webpage.url)
                pass

        except requests.Timeout as e:
            self.error += "Timeout in scrape_page_for_open_location on {}: {}".format(
                my_webpage, str(e))
            logger.info(self.error)
        except requests.exceptions.ConnectionError as e:
            self.error += "ConnectionError in scrape_page_for_open_location on {}: {}".format(
                my_webpage, str(e))
            logger.info(self.error)
        except requests.exceptions.ChunkedEncodingError as e:
            self.error += "ChunkedEncodingError in scrape_page_for_open_location on {}: {}".format(
                my_webpage, str(e))
            logger.info(self.error)
        except requests.exceptions.RequestException as e:
            self.error += "RequestException in scrape_page_for_open_location on {}: {}".format(
                my_webpage, str(e))
            logger.info(self.error)
        except etree.XMLSyntaxError as e:
            self.error += "XMLSyntaxError in scrape_page_for_open_location on {}: {}".format(
                my_webpage, str(e))
            logger.info(self.error)
        except Exception:
            logger.exception("Exception in scrape_page_for_open_location")
            self.error += "Exception in scrape_page_for_open_location"
            logger.info(self.error)

    def should_scrape_publisher_page(self):
        if self.genre == 'journal':
            return False

        return True

    def should_look_for_publisher_pdf(self):
        if self.genre == 'book':
            if self.is_same_publisher('Université Paris Cité'):
                return True
            else:
                return False

        if self.issn_l in [
            # landing page has pdfs for every article in issue
            '1818-5487',  # Aquatic Invasions
            '2072-5981',  # Magnetic resonance in solids
            '1989-8649',  # Management of Biological Invasions
            '2164-3989',  # The Professional Counselor
            '0970-9274',  # Journal of Human Ecology
            '0973-5070',  # STUDIES ON ETHNO-MEDICINE
            # in doaj, PDF has full issue so landing page is more specific
            '2471-190X',  # Open Rivers: Rethinking Water, Place & Community
            '0097-6156',  # Books
            # in doaj, doi leads to current issue so PDF is useless
            '0210-6124',
            # Atlantis. Journal of the Spanish Association for Anglo-American Studies
        ]:
            return False

        if self.issn_l == '0007-0610' and self.year <= 1999:
            # British Dental Journal, https://www.nature.com/articles/4806453.pdf
            return False

        return True

    def elsevier_bronze_exception_func(self, license):
        def func():
            if license != 'publisher-specific-oa':
                return False
            elif ('elsevier' in self.publisher.lower() or (
                    self.resolved_doi_url and 'sciencedirect.com' in self.resolved_doi_url) or self.doi.startswith(
                '10.1016')):
                return True
            rows = db.session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM journal WHERE issn_l = :issn_l AND publisher = 'Elsevier')"),
                                      {'issn_l': self.issn_l})
            return rows.rowcount > 0

        return func

    def set_title_hacks(self):
        workaround_titles = {
            # these preprints doesn't have the same title as the doi
            # eventually solve these by querying arxiv like this:
            # http://export.arxiv.org/api/query?search_query=doi:10.1103/PhysRevD.89.085017
            "10.1016/j.astropartphys.2007.12.004": "In situ radioglaciological measurements near Taylor Dome, Antarctica and implications for UHE neutrino astronomy",
            "10.1016/s0375-9601(02)01803-0": "Universal quantum computation using only projective measurement, quantum memory, and preparation of the 0 state",
            "10.1103/physreva.65.062312": "An entanglement monotone derived from Grover's algorithm",

            # crossref has title "aol" for this
            # set it to real title
            "10.1038/493159a": "Altmetrics: Value all research products",

            # crossref has no title for this
            "10.1038/23891": "Complete quantum teleportation using nuclear magnetic resonance",

            # is a closed-access datacite one, with the open-access version in BASE
            # need to set title here because not looking up datacite titles yet (because ususally open access directly)
            "10.1515/fabl.1988.29.1.21": "Thesen zur Verabschiedung des Begriffs der 'historischen Sage'",

            # preprint has a different title
            "10.1123/iscj.2016-0037": "METACOGNITION AND PROFESSIONAL JUDGMENT AND DECISION MAKING: IMPORTANCE, APPLICATION AND EVALUATION",

            # preprint has a different title
            "10.1038/s41477-017-0066-9": "Low Rate of Somatic Mutations in a Long-Lived Oak Tree",
            "10.1101/2020.08.10.238428": "Cell-programmed nutrient partitioning in the tumour microenvironment",

            # mysteriously missing from crossref now
            "10.1093/annweh/wxy044": "Development of and Selected Performance Characteristics of CANJEM, a General Population Job-Exposure Matrix Based on Past Expert Assessments of Exposure"
        }

        if self.doi in workaround_titles:
            self.title = workaround_titles[self.doi]
            self.normalized_title = normalize_title(self.title)

    def set_license_hacks(self):
        if self.fulltext_url and "harvard.edu/" in self.fulltext_url:
            if not self.license or self.license == "unknown":
                self.license = "cc-by-nc"

    @property
    def crossref_alternative_id(self):
        try:
            return re.sub(r"\s+", " ",
                          self.crossref_api_raw_new["alternative-id"][0])
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def publisher(self):
        try:
            return re.sub("\s+", " ", self.crossref_api_modified["publisher"])
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def volume(self):
        try:
            return self.crossref_api_raw_new["volume"]
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def issue(self):
        try:
            return self.crossref_api_raw_new["issue"]
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def first_page(self):
        try:
            return self.crossref_api_raw_new["page"].split('-')[0]
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def last_page(self):
        try:
            return self.crossref_api_raw_new["page"].split('-')[-1]
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def earliest_date(self):
        dts = [self.issued, self.crossref_published, self.approved, self.created, self.deposited]
        return min([dt for dt in dts if dt is not None], default=None)

    @property
    def issued(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in \
                    self.crossref_api_raw_new["issued"]:
                date_parts = self.crossref_api_raw_new["issued"]["date-parts"][
                    0]
                return get_citeproc_date(*date_parts)
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def crossref_published(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in \
                    self.crossref_api_raw_new["published"]:
                date_parts = \
                    self.crossref_api_raw_new["published"]["date-parts"][0]
                return get_citeproc_date(*date_parts)
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def deposited(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in \
                    self.crossref_api_raw_new["deposited"]:
                date_parts = \
                    self.crossref_api_raw_new["deposited"]["date-parts"][0]
                return get_citeproc_date(*date_parts)
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def approved(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in \
                    self.crossref_api_raw_new["approved"]:
                date_parts = \
                    self.crossref_api_raw_new["approved"]["date-parts"][0]
                return get_citeproc_date(*date_parts)
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def created(self):
        try:
            if self.crossref_api_raw_new and "date-parts" in \
                    self.crossref_api_raw_new["created"]:
                date_parts = self.crossref_api_raw_new["created"]["date-parts"][
                    0]
                return get_citeproc_date(*date_parts)
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def crossref_text_mining_pdf(self):
        try:
            for link in self.crossref_api_modified['link']:
                if (
                        link['content-version'] == 'vor' and
                        link['intended-application'] == 'text-mining' and
                        link['content-version'] == 'application/pdf'
                ):
                    return link['URL']
        except (KeyError, TypeError, AttributeError):
            return None

    @property
    def open_manuscript_licenses(self):
        try:
            license_dicts = self.crossref_api_modified["license"]
            author_manuscript_urls = []

            for license_dict in license_dicts:
                if license_dict[
                    "URL"] in oa_local.closed_manuscript_license_urls():
                    continue

                license_date = None
                if license_dict.get("content-version", None):
                    if license_dict["content-version"] == "am":
                        if license_dict.get("start", None):
                            if license_dict["start"].get("date-time", None):
                                license_date = license_dict["start"][
                                    "date-time"]

                        try:
                            license_date = license_date and dateutil.parser.parse(
                                license_date).date()
                            license_date += self._author_manuscript_delay()
                        except Exception:
                            license_date = None

                        author_manuscript_urls.append(
                            {'url': license_dict["URL"], 'date': license_date})

            return sorted(author_manuscript_urls, key=lambda amu: amu['date'])
        except (KeyError, TypeError):
            return []

    def _author_manuscript_delay(self):
        if self.is_same_publisher(
                'Institute of Electrical and Electronics Engineers (IEEE)'):
            # policy says 2 years after publication but license date is date of publication
            return datetime.timedelta(days=365 * 2)
        else:
            return datetime.timedelta()

    @property
    def crossref_licenses(self):
        unspecified_version_publishers = [
            'Informa UK Limited',
            'Geological Society of London',
        ]
        allow_unspecified = any(
            [self.is_same_publisher(p) for p in unspecified_version_publishers])

        tdm_publishers = [
            'Uniwersytet Jagiellonski - Wydawnictwo Uniwersytetu Jagiellonskiego']
        allow_tdm = any([self.is_same_publisher(p) for p in tdm_publishers])

        try:
            license_dicts = self.crossref_api_modified["license"]
            license_urls = []

            for license_dict in license_dicts:
                license_date = None

                if license_version := license_dict.get("content-version", None):
                    if (
                            license_version == "vor"
                            or (
                            allow_unspecified and license_version == "unspecified")
                            or (allow_tdm and license_version == "tdm")
                    ):
                        if license_dict.get("start", None):
                            if license_dict["start"].get("date-time", None):
                                license_date = license_dict["start"].get(
                                    "date-time", None)

                        try:
                            license_date = license_date and dateutil.parser.parse(
                                license_date).date()
                        except Exception:
                            license_date = None

                        license_urls.append(
                            {'url': license_dict["URL"], 'date': license_date})

            return sorted(license_urls, key=lambda license: license['date'])
        except (KeyError, TypeError):
            return []

    @property
    def is_subscription_journal(self):
        if (
                oa_local.is_open_via_doaj(self.issns, self.all_journals,
                                          self.year)
                or oa_local.is_open_via_doi_fragment(self.doi)
                or oa_local.is_open_via_publisher(self.publisher)
                or self.is_open_journal_via_observed_oa_rate()
                or oa_local.is_open_via_manual_journal_setting(self.issns,
                                                               self.year)
                or oa_local.is_open_via_url_fragment(self.url)
        ):
            return False
        return True

    @property
    def doi_resolver(self):
        if not self.doi:
            return None
        if oa_local.is_open_via_datacite_prefix(self.doi):
            return "datacite"
        if self.crossref_api_modified and "error" not in self.crossref_api_modified:
            return "crossref"
        return None

    @property
    def is_free_to_read(self):
        return bool(self.fulltext_url)

    @property
    def is_boai_license(self):
        boai_licenses = ["cc-by", "cc0", "pd"]
        if self.license and (self.license in boai_licenses):
            return True
        return False

    @property
    def authors(self):
        try:
            return self.crossref_api_modified["all_authors"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def first_author_lastname(self):
        try:
            return self.crossref_api_modified["first_author_lastname"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def last_author_lastname(self):
        try:
            last_author = self.authors[-1]
            return last_author["family"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def display_issns(self):
        if self.issns:
            return ",".join(self.issns)
        return None

    @property
    def issns(self):
        issns = []
        try:
            issns = self.crossref_api_modified["issn"]
        except (AttributeError, TypeError, KeyError):
            try:
                issns = self.crossref_api_modified["issn"]
            except (AttributeError, TypeError, KeyError):
                if self.tdm_api:
                    issns = re.findall("<issn media_type=.*>(.*)</issn>",
                                       self.tdm_api)
        if not issns:
            return None
        else:
            return issns

    @property
    def best_title(self):
        if hasattr(self, "title") and self.title:
            return re.sub("\s+", " ", self.title)
        return self.crossref_title

    @property
    def crossref_title(self):
        try:
            return re.sub("\s+", " ", self.crossref_api_modified["title"])
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def year(self):
        try:
            return self.crossref_api_modified["year"]
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def journal(self):
        try:
            return re.sub("\s+", " ", self.crossref_api_modified["journal"])
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def all_journals(self):
        try:
            return self.crossref_api_modified["all_journals"]
        except (AttributeError, TypeError, KeyError, IndexError):
            return None

    @property
    def genre(self):
        try:
            return re.sub("\s+", " ", self.crossref_api_modified["type"])
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def abstract_from_crossref(self):
        try:
            return self.crossref_api_raw_new["abstract"]
        except (AttributeError, TypeError, KeyError):
            return None

    @property
    def deduped_sorted_locations(self):
        locations = []
        sorted_locations = self.sorted_locations

        # transfer PDF URLs from bronze location to hybrid location
        # then best_url is the same, and they aren't duplicated
        # be very conservative - only merge if exactly one location with pdf and one without,
        # and both are published versions hosted by the publisher

        publisher_no_pdf = [
            loc for loc in sorted_locations
            if
            loc.host_type == "publisher" and loc.version == "publishedVersion" and not loc.pdf_url
        ]

        publisher_pdf = [
            loc for loc in sorted_locations
            if
            loc.host_type == "publisher" and loc.version == "publishedVersion" and loc.pdf_url
        ]

        if len(publisher_no_pdf) == 1 and len(publisher_pdf) == 1:
            if publisher_no_pdf[0].metadata_url == publisher_pdf[
                0].metadata_url:
                publisher_no_pdf[0].pdf_url = publisher_pdf[0].pdf_url

        for next_location in sorted_locations:
            urls_so_far = [location.best_url for location in locations]
            if next_location.best_url not in urls_so_far:
                locations.append(next_location)
        return locations

    @property
    def filtered_locations(self):
        locations = self.open_locations

        # now remove noncompliant ones
        compliant_locations = [location for location in locations if
                               not location.is_reported_noncompliant]

        validate_pdf_urls(compliant_locations)
        valid_locations = [
            x for x in compliant_locations
            if x.pdf_url_valid
               and not (self.has_bad_doi_url and x.best_url == self.url)
               and x.endpoint_id != '01b84da34b861aa938d'  # lots of abstracts presented as full text. find a better way to do this.
               and x.endpoint_id != '58e562cef9eb07c3c1d'
            # garbage PDFs in identifier tags
        ]

        for location in valid_locations:
            if location.pdf_url:
                location.pdf_url = fix_url_scheme(location.pdf_url)

            if location.metadata_url:
                location.metadata_url = fix_url_scheme(location.metadata_url)

        return valid_locations

    @property
    def sorted_locations(self):
        locations = self.filtered_locations
        # first sort by best_url so ties are handled consistently
        locations = sorted(locations, key=lambda x: x.best_url, reverse=False)
        # now sort by what's actually better
        locations = sorted(locations, key=lambda x: x.sort_score, reverse=False)
        return locations

    @property
    def data_standard(self):
        if self.scrape_updated and not self.error:
            return 2
        else:
            return 1

    def lookup_issn_l(self):
        for issn in self.issns or []:
            # use the first issn that matches an issn_l
            # can't really do anything if they would match different issn_ls
            lookup = db.session.query(IssnlLookup).get(issn)
            if lookup:
                return lookup

        return None

    def lookup_journal(self):
        return self.issn_l and db.session.query(Journal).options(
            orm.defer('api_raw_crossref'), orm.defer('api_raw_issn')
        ).get({'issn_l': self.issn_l})

    def get_resolved_url(self):
        if hasattr(self, "my_resolved_url_cached"):
            return self.my_resolved_url_cached
        try:
            r = requests.get("http://doi.org/{}".format(self.id),
                             stream=True,
                             allow_redirects=True,
                             timeout=(3, 3),
                             verify=False
                             )

            self.my_resolved_url_cached = r.url

        except Exception:  # hardly ever do this, but man it seems worth it right here
            logger.exception("get_resolved_url failed")
            self.my_resolved_url_cached = None

        return self.my_resolved_url_cached

    def __repr__(self):
        if self.id:
            my_string = self.id
        else:
            my_string = self.best_title
        return "<Pub ( {} )>".format(my_string)

    @property
    def reported_noncompliant_copies(self):
        return reported_noncompliant_url_fragments(self.doi)

    def is_same_publisher(self, publisher):
        if self.publisher:
            return normalize(self.publisher) == normalize(publisher)
        return False

    @property
    def best_url(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.best_url

    @property
    def best_url_is_pdf(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.best_url_is_pdf

    @property
    def best_evidence(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.display_evidence

    @property
    def best_host(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.host_type

    @property
    def best_repo_id(self):
        if self.best_host != 'repository':
            return None
        return self.best_oa_location.endpoint_id

    @property
    def best_license(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.license

    @property
    def best_version(self):
        if not self.best_oa_location:
            return None
        return self.best_oa_location.version

    @property
    def best_oa_location_dict(self):
        best_location = self.best_oa_location
        if best_location:
            return best_location.to_dict_v2()
        return None

    @property
    def best_oa_location(self):
        all_locations = [location for location in self.all_oa_locations]
        if all_locations:
            return all_locations[0]
        return None

    @property
    def first_oa_location_dict(self):
        first_location = self.first_oa_location
        if first_location:
            return first_location.to_dict_v2()
        return None

    @property
    def first_oa_location(self):
        all_locations = [location for location in self.all_oa_locations]
        if all_locations:
            return sorted(all_locations, key=lambda loc: (
                loc.oa_date or datetime.date.max, loc.sort_score))[0]
        return None

    @property
    def all_oa_locations(self):
        all_locations = [location for location in self.deduped_sorted_locations]
        if all_locations:
            for location in all_locations:
                location.is_best = False
            all_locations[0].is_best = True
        return all_locations

    def all_oa_location_dicts(self):
        return [location.to_dict_v2() for location in self.all_oa_locations]

    def embargoed_oa_location_dicts(self):
        return [location.to_dict_v2() for location in self.embargoed_locations]

    def to_dict_v1(self):
        response = {
            "algorithm_version": self.data_standard,
            "doi_resolver": self.doi_resolver,
            "evidence": self.evidence,
            "free_fulltext_url": self.fulltext_url,
            "is_boai_license": self.is_boai_license,
            "is_free_to_read": self.is_free_to_read,
            "is_subscription_journal": self.is_subscription_journal,
            "license": self.license,
            "oa_color": self.oa_status and self.oa_status.value,
            "reported_noncompliant_copies": self.reported_noncompliant_copies
        }

        for k in ["doi", "title", "url"]:
            value = getattr(self, k, None)
            if value:
                response[k] = value

        if self.error:
            response["error"] = self.error

        return response

    @property
    def best_location(self):
        if not self.deduped_sorted_locations:
            return None
        return self.deduped_sorted_locations[0]

    @property
    def is_archived_somewhere(self):
        if self.is_oa:
            return any([location.oa_status is OAStatus.green for location in
                        self.deduped_sorted_locations])
        return None

    @property
    def oa_is_doaj_journal(self):
        if self.is_oa:
            if oa_local.is_open_via_doaj(self.issns, self.all_journals,
                                         self.year):
                return True
            else:
                return False
        return False

    @property
    def oa_is_open_journal(self):
        if self.is_oa:
            if self.oa_is_doaj_journal:
                return True
            if oa_local.is_open_via_publisher(self.publisher):
                return True
            if oa_local.is_open_via_manual_journal_setting(self.issns,
                                                           self.year):
                return True
            if self.is_open_journal_via_observed_oa_rate():
                return True
            if oa_local.is_open_via_publisher_genre(self.publisher, self.genre):
                return True
            if oa_local.is_open_via_journal_doi_prefix(self.doi):
                return True
        return False

    @property
    def display_updated(self):
        if self.updated:
            return self.updated.isoformat()
        return None

    @property
    def has_abstract(self):
        if self.abstracts:
            return True
        return False

    @property
    def display_abstracts(self):
        return []

    @property
    def predicted_bronze_embargo_end(self):
        published = self.issued or self.deposited or datetime.date(1970, 1, 1)
        journal = self.lookup_journal()

        if journal and journal.embargo and journal.embargo + published > datetime.date.today():
            return journal.embargo + published

        return None

    @property
    def refresh_priority(self):
        published = self.issued or self.deposited or datetime.date(1970, 1, 1)
        today = datetime.date.today()
        journal = self.lookup_journal()
        current_oa_status = self.response_jsonb and self.response_jsonb.get(
            'oa_status', None)

        if (
                current_oa_status
                and (
                current_oa_status == "gold" or current_oa_status == "hybrid")
                and self.resolved_doi_http_status is not None
        ):
            return -1.555
        elif published > datetime.date.today():
            # refresh things that aren't published yet infrequently
            refresh_interval = datetime.timedelta(days=365)
        else:
            if self.oa_status in [OAStatus.closed, OAStatus.green]:
                # look for delayed-OA articles after common embargo periods by adjusting the published date
                if journal and journal.embargo and journal.embargo + published < today:
                    # article is past known embargo period
                    if not self.scrape_metadata_url:
                        published += journal.embargo

                elif journal and journal.delayed_oa and not self.scrape_metadata_url:
                    # treat every 6th mensiversary for the first 4 years like the publication date
                    six_months = relativedelta(months=6)

                    shifts = 0
                    while shifts < 8 and published < today - six_months:
                        published += six_months
                        shifts += 1

            age = today - published

            # arbitrary scale factor, refresh newer things more often
            refresh_interval = age / 6

        if self.genre == 'component':
            refresh_interval *= 2

        refresh_interval = clamp(refresh_interval, datetime.timedelta(days=2),
                                 datetime.timedelta(days=365))

        last_refresh = self.scrape_updated or datetime.datetime(1970, 1, 1)
        since_last_refresh = datetime.datetime.utcnow() - last_refresh

        priority = (
                           since_last_refresh - refresh_interval).total_seconds() / refresh_interval.total_seconds()
        return priority

    @property
    def has_bad_doi_url(self):
        return (
                (self.issns and (
                    # links don't resolve
                        '1507-1367' in self.issns or
                        # links don't resolve
                        '2237-0722' in self.issns
                )) or
                # pdf abstracts
                self.id.startswith('10.5004/dwt.') or
                self.id == '10.2478/cirr-2019-0007'
        )

    def is_open_journal_via_observed_oa_rate(self):
        lookup = self.issn_l and db.session.query(JournalOaStartYear).get(
            {'issn_l': self.issn_l})
        return lookup and self.issued and self.issued.year >= lookup.oa_year

    def store_refresh_priority(self):
        logger.info(
            f"Setting refresh priority for {self.id} to {self.refresh_priority}")
        stmt = sql.text(
            'update pub_refresh_queue set priority = :priority where id = :id'
        ).bindparams(priority=self.refresh_priority, id=self.id)
        db.session.execute(stmt)

    def store_preprint_relationships(self):
        preprint_relationships = []

        if self.crossref_api_raw_new and self.crossref_api_raw_new.get(
                'relation', None):
            postprints = self.crossref_api_raw_new['relation'].get(
                'is-preprint-of', [])
            postprint_dois = [p.get('id', None) for p in postprints if
                              p.get('id-type', None) == 'doi']
            for postprint_doi in postprint_dois:
                try:
                    normalized_postprint_doi = normalize_doi(postprint_doi)
                    preprint_relationships.append({'preprint_id': self.doi,
                                                   'postprint_id': normalized_postprint_doi})
                except Exception:
                    pass

            preprints = self.crossref_api_raw_new['relation'].get(
                'has-preprint', [])
            preprint_dois = [p.get('id', None) for p in preprints if
                             p.get('id-type', None) == 'doi']
            for preprint_doi in preprint_dois:
                try:
                    normalized_preprint_doi = normalize_doi(preprint_doi)
                    preprint_relationships.append(
                        {'preprint_id': normalized_preprint_doi,
                         'postprint_id': self.doi})
                except Exception:
                    pass

        for preprint_relationship in preprint_relationships:
            db.session.merge(Preprint(**preprint_relationship))

    def store_retractions(self):
        retracted_dois = set()

        if self.crossref_api_raw_new:
            for update_to in self.crossref_api_raw_new.get('update-to', []):
                if update_to.get('type') == 'retraction':
                    if retracted_doi := normalize_doi(update_to.get('DOI'),
                                                      return_none_if_error=True):
                        retracted_dois.add(retracted_doi)

        db.session.query(Retraction).filter(
            Retraction.retraction_doi == self.doi,
            Retraction.retracted_doi.notin_(list(retracted_dois))
        ).delete()

        for retracted_doi in retracted_dois:
            db.session.merge(Retraction(retraction_doi=self.doi,
                                        retracted_doi=retracted_doi))

    def store_or_remove_pdf_urls_for_validation(self):
        """Store PDF URLs for validation."""
        urls_to_add = set()
        for loc in self.open_locations:
            if loc.pdf_url and not is_pmc(loc.pdf_url):
                urls_to_add.add(loc.pdf_url)

        for url in urls_to_add:
            try:
                db.session.merge(
                    PdfUrl(url=url, publisher=self.publisher)
                )
                db.session.commit()
            except (IntegrityError, UniqueViolation):
                db.session.rollback()
                logger.info(f"Integrity error for {url} in {self.id}")

    def mint_pages(self):
        for p in oa_page.make_oa_pages(self):
            db.session.merge(p)

    @staticmethod
    def dict_v2_fields():
        return OrderedDict([
            ("doi", lambda p: p.doi),
            ("doi_url", lambda p: p.url),
            ("title", lambda p: p.best_title),
            ("genre", lambda p: p.genre),
            ("is_paratext", lambda p: p.is_paratext),
            ("published_date", lambda p: p.issued and p.issued.isoformat()),
            ("year", lambda p: p.year),
            ("journal_name", lambda p: p.journal),
            ("journal_issns", lambda p: p.display_issns),
            ("journal_issn_l", lambda p: p.issn_l),
            ("journal_is_oa", lambda p: p.oa_is_open_journal),
            ("journal_is_in_doaj", lambda p: p.oa_is_doaj_journal),
            ("publisher", lambda p: p.publisher),
            ("is_oa", lambda p: p.is_oa),
            ("oa_status", lambda p: p.oa_status and p.oa_status.value),
            ("has_repository_copy", lambda p: p.has_green),
            ("best_oa_location", lambda p: p.best_oa_location_dict),
            ("first_oa_location", lambda p: p.first_oa_location_dict),
            ("oa_locations", lambda p: p.all_oa_location_dicts()),
            ("oa_locations_embargoed",
             lambda p: p.embargoed_oa_location_dicts()),
            ("updated", lambda p: p.display_updated),
            ("data_standard", lambda p: p.data_standard),
            ("z_authors", lambda p: p.authors),
        ])

    def to_dict_v2(self):
        response = OrderedDict(
            [(key, func(self)) for key, func in Pub.dict_v2_fields().items()])
        return response

    def to_dict_search(self):

        response = self.to_dict_v2()

        response["abstracts"] = self.display_abstracts

        del response["z_authors"]
        if self.authors:
            response["author_lastnames"] = [author.get("family", None) for
                                            author in self.authors]
        else:
            response["author_lastnames"] = []

        if not hasattr(self, "score"):
            self.score = None
        response["score"] = self.score

        if not hasattr(self, "snippet"):
            self.snippet = None
        response["snippet"] = self.snippet

        return response

# db.create_all()
# commit_success = safe_commit(db)
# if not commit_success:
#     logger.info(u"COMMIT fail making objects")
