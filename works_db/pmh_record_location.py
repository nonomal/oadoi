import datetime
from urllib.parse import quote, urlparse

from app import db
from works_db.location import Location


class PmhRecordLocation(Location):
    __tablename__ = None

    pmh_id = db.Column(db.Text)

    __mapper_args__ = {
        "polymorphic_identity": "pmh_record"
    }

    @staticmethod
    def from_pmh_record(pmh_record):
        if not pmh_record:
            return None

        best_page = PmhRecordLocation.best_page(pmh_record)
        if not best_page:
            return None

        location = PmhRecordLocation.query.filter(PmhRecordLocation.pmh_id == pmh_record.id).scalar()

        if not location:
            location = PmhRecordLocation()

        location.pmh_id = pmh_record.id

        location.title = pmh_record.title
        location.authors = [{"raw": author} for author in pmh_record.authors] if pmh_record.authors else None
        location.doi = pmh_record.doi

        if best_page.landing_page_archive_url():
            location.record_webpage_url = best_page.scrape_metadata_url
        else:
            location.record_webpage_url = None

        location.record_webpage_archive_url = best_page.landing_page_archive_url()
        location.record_structured_url = best_page.get_pmh_record_url()
        location.record_structured_archive_url = f'https://api.unpaywall.org/pmh_record_xml/{quote(pmh_record.id)}'

        location.work_pdf_url = best_page.scrape_pdf_url
        location.work_pdf_archive_url = best_page.fulltext_pdf_archive_url()
        location.is_work_pdf_url_free_to_read = True if best_page.scrape_pdf_url else None

        location.is_oa = bool(best_page.is_open)

        if location.is_oa:
            best_page_first_available = best_page.first_available
            if isinstance(best_page_first_available, datetime.date):
                location.oa_date = datetime.datetime.combine(
                    best_page_first_available,
                    datetime.datetime.min.time()
                )
            else:
                location.oa_date = best_page_first_available

            location.open_license = best_page.scrape_license
            location.open_version = best_page.scrape_version
        else:
            location.oa_date = None
            location.open_license = None
            location.open_version = None

        if db.session.is_modified(location):
            location.updated = datetime.datetime.utcnow().isoformat()

        return location

    @staticmethod
    def best_page(pmh_record):
        def repo_host_match_score(score_page):
            repo_host = None
            if score_page.endpoint and score_page.endpoint.pmh_url:
                repo_host = urlparse(score_page.endpoint.pmh_url).hostname

            if not repo_host:
                return 0

            page_host = urlparse(score_page.url).hostname
            page_host_parts = list(reversed(page_host.split('.')))
            repo_host_parts = list(reversed(repo_host.split('.')))

            match_score = 0
            for i in range(0, min(len(page_host_parts), len(repo_host_parts))):
                if repo_host_parts[i] == page_host_parts[i]:
                    match_score += 1

            return match_score

        ranked_pages = sorted(
            pmh_record.pages,
            key=lambda page: (
                page.scrape_metadata_url is not None,
                page.scrape_pdf_url is not None,
                page.scrape_metadata_url != page.scrape_pdf_url,
                repo_host_match_score(page)
            )
        )

        return ranked_pages[-1]
