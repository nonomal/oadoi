import datetime
from urllib.parse import quote

from app import db
from recordthresher.record import Record


class CrossrefDoiRecord(Record):
    __tablename__ = None

    __mapper_args__ = {'polymorphic_identity': 'crossref_doi'}

    @staticmethod
    def from_pub(pub):
        record = CrossrefDoiRecord.query.filter(CrossrefDoiRecord.doi == pub.id).scalar()

        if not record:
            record = CrossrefDoiRecord()

        record.title = pub.title
        record.authors = pub.authors
        record.doi = pub.id

        record.record_webpage_url = pub.url
        record.journal_issn_l = pub.issn_l

        record.record_webpage_archive_url = pub.landing_page_archive_url() if pub.doi_landing_page_is_archived else None

        record.record_structured_url = f'https://api.crossref.org/v1/works/{quote(pub.id)}'
        record.record_structured_archive_url = f'https://api.unpaywall.org/crossref_api_cache/{quote(pub.id)}'

        if pub.best_oa_location and pub.best_oa_location.metadata_url == pub.url:
            record.work_pdf_url = pub.best_oa_location.pdf_url
            record.is_work_pdf_url_free_to_read = True if pub.best_oa_location.pdf_url else None
            record.is_oa = pub.best_oa_location is not None

            if isinstance(pub.best_oa_location.oa_date, datetime.date):
                record.oa_date = datetime.datetime.combine(
                    pub.best_oa_location.oa_date,
                    datetime.datetime.min.time()
                )
            else:
                record.oa_date = pub.best_oa_location.oa_date

            record.open_license = pub.best_oa_location.license
            record.open_version = pub.best_oa_location.version
        else:
            record.work_pdf_url = None
            record.is_work_pdf_url_free_to_read = None
            record.is_work_pdf_url_free_to_read = None
            record.is_oa = False
            record.oa_date = None
            record.open_license = None
            record.open_version = None

        if db.session.is_modified(record):
            record.updated = datetime.datetime.utcnow().isoformat()

        return record