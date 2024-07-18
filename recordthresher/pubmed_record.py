import datetime
import hashlib
import json
import re
import uuid

import dateutil.parser
import shortuuid
from lxml import etree

from app import db
from app import logger
from journal import Journal
from recordthresher.pubmed import PubmedAffiliation, PubmedArticleType, PubmedAuthor
from recordthresher.pubmed import PubmedReference, PubmedMesh, PubmedWork
from recordthresher.record import Record
from recordthresher.util import ARXIV_ID_PATTERN, normalize_author, normalize_citation
from util import clean_doi, normalize_title


class PubmedRecord(Record):
    __tablename__ = None

    __mapper_args__ = {
        "polymorphic_identity": "pubmed_record"
    }

    @staticmethod
    def from_pmid(pmid):
        if not pmid:
            return None

        if not (pubmed_work := PubmedWork.query.get(pmid)):
            return None

        record_id = shortuuid.encode(
            uuid.UUID(bytes=hashlib.sha256(f'pubmed_record:{pmid}'.encode('utf-8')).digest()[0:16])
        )

        record = PubmedRecord.query.get(record_id)

        if not record:
            record = PubmedRecord(id=record_id)

        record.pmid = pmid
        record.title = pubmed_work.article_title
        record.normalized_title = normalize_title(record.title)
        record.abstract = pubmed_work.abstract or None

        work_tree = etree.fromstring(pubmed_work.pubmed_article_xml)

        pub_date, pub_year, pub_month, pub_day = None, None, '1', '1'

        pub_date_year_element = work_tree.find('.//PubDate/Year')

        if pub_date_year_element is None:
            pub_date_year_element = work_tree.find('.//PubMedPubDate[@PubStatus="pubmed"]/Year')

        if pub_date_year_element is not None:
            pub_date_element = pub_date_year_element.getparent()
        else:
            pub_date_element = None

        if pub_date_element is not None:
            if (year_element := pub_date_element.find('.//Year')) is not None:
                pub_year = year_element.text
            if (month_element := pub_date_element.find('.//Month')) is not None:
                pub_month = month_element.text
            if (day_element := pub_date_element.find('.//Day')) is not None:
                pub_day = day_element.text

        if pub_year:
            pub_date = dateutil.parser.parse(f'{pub_year} {pub_month} {pub_day}')
        else:
            logger.error(f"can't determine a published date, not making a record")
            return None

        record.published_date = pub_date.date()

        if (article_type_elements := work_tree.findall('.//PublicationTypeList/PublicationType')) is not None:
            article_type_names = [e.text for e in article_type_elements]

            # list of publication types to prioritize. order is important
            pubmed_publication_types_to_prioritize = [
                'Published Erratum',
                'Systematic Review',
                'Meta-Analysis',
                'Review',
                'Letter',
                'Comment',
                'Editorial',
                'Retraction of Publication',
                'Preprint',
                'Journal Article',
            ]
            best_type = None
            for pubmed_type in pubmed_publication_types_to_prioritize:
                if pubmed_type in article_type_names:
                    best_type = pubmed_type
                    break
            if best_type is not None:
                record.genre = best_type
            elif best_type is None and len(article_type_names) > 0:
                # this is if the above didn't come up with anything.
                # fall back to the old way of doing things: pick based on a static table with types ranked by relative frequency
                normalized_names = {}

                for article_type_name in article_type_names:
                    normalized_names[article_type_name.strip().lower()] = article_type_name

                best_type = PubmedArticleType.query.filter(
                    PubmedArticleType.article_type.in_(normalized_names.keys())
                ).order_by(PubmedArticleType.rank).first()

                if best_type:
                    record.genre = normalized_names[best_type.article_type]
                elif article_type_names:
                    record.genre = article_type_names[0]
                else:
                    record.genre = None
        else:
            record.genre = None

        if (volume_element := work_tree.find('.//Article/Journal/JournalIssue/Volume')) is not None:
            record.volume = volume_element.text

        if (issue_element := work_tree.find('.//Article/Journal/JournalIssue/Issue')) is not None:
            record.issue = issue_element.text

        if (pagination_element := work_tree.find('.//Article/Pagination/MedlinePgn')) is not None:
            if pagination_text := pagination_element.text:
                record.first_page = pagination_text.split('-')[0]
                record.last_page = pagination_text.split('-')[-1]

        PubmedRecord.set_journal_info(record, work_tree)

        retraction = work_tree.find('.//CommentsCorrections[@RefType="RetractionIn"]')
        record.is_retracted = retraction is not None

        record_authors = []
        pubmed_authors = PubmedAuthor.query.filter(PubmedAuthor.pmid == pmid).all()
        for pubmed_author in pubmed_authors:
            record_author = {
                'sequence': 'first' if pubmed_author.author_order == 1 else 'additional',
                'family': pubmed_author.family,
                'orcid': pubmed_author.orcid,
                'given': pubmed_author.given or pubmed_author.initials,
                'affiliation': []
            }

            pubmed_affiliations = PubmedAffiliation.query.filter(
                PubmedAffiliation.pmid == pmid, PubmedAffiliation.author_order == pubmed_author.author_order
            ).order_by(
                PubmedAffiliation.affiliation_number
            ).all()

            for pubmed_affiliation in pubmed_affiliations:
                record_author['affiliation'].append({'name': pubmed_affiliation.affiliation})

            record_authors.append(normalize_author(record_author))

        record.authors = record_authors

        record_citations = []
        pubmed_references = PubmedReference.query.filter(PubmedReference.pmid == pmid).all()
        for pubmed_reference in pubmed_references:
            record_citation = {'unstructured': pubmed_reference.citation}

            try:
                citation_tree = etree.fromstring(pubmed_reference.reference)
                if (cited_pmid_element := citation_tree.find('./ArticleIdList/ArticleId[@IdType="pubmed"]')) is not None:
                    if cited_pmid_element.text:
                        record_citation['pmid'] = cited_pmid_element.text
            except etree.XMLSyntaxError:
                pass

            record_citations.append(normalize_citation(record_citation))

        record.citations = record_citations

        mesh = [
            {
                'descriptor_ui': m.descriptor_ui,
                'descriptor_name': m.descriptor_name,
                'qualifier_ui': m.qualifier_ui,
                'qualifier_name': m.qualifier_name,
                'is_major_topic': m.is_major_topic,
            } for m in PubmedMesh.query.filter(PubmedMesh.pmid == pmid).all()
        ]

        record.mesh = mesh

        record.doi = clean_doi(pubmed_work.doi, return_none_if_error=True)
        record.pmcid = pubmed_work.pmcid
        record.record_webpage_url = f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'
        record.record_structured_url = f'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml'
        record.record_structured_archive_url = f'https://api.unpaywall.org/pubmed_xml/{pmid}'

        pii_location_id_elements = work_tree.findall('.//Article/ELocationID[@EIdType="pii"]')

        for pii_location_id_element in pii_location_id_elements:
            if pii_text := (pii_location_id_element.text and pii_location_id_element.text.strip()):
                if re.match(f'^{ARXIV_ID_PATTERN}$', pii_text):
                    record.arxiv_id = pii_text

        record.flag_modified_jsonb()

        record.authors = json.dumps(record.authors)
        record.mesh = json.dumps(record.mesh)
        record.citations = json.dumps(record.citations)

        if db.session.is_modified(record):
            record.updated = datetime.datetime.utcnow().isoformat()

        return record

    @staticmethod
    def set_journal_info(record, work_tree):
        lookup_issns = []

        if (issn_l_element := work_tree.find('./MedlineCitation/MedlineJournalInfo/ISSNLinking')) is not None:
            if issn_l_element.text:
                # may be overridden later, but if we get an explicit ISSN-L use it for now
                record.journal_issn_l = issn_l_element.text
            lookup_issns.append(issn_l_element.text)

        if (journal_element := work_tree.find('./MedlineCitation/Article/Journal')) is not None:
            if (e_issn_element := journal_element.find('./ISSN[@IssnType="Electronic"]')) is not None:
                lookup_issns.append(e_issn_element.text)
            if (print_issn_element := journal_element.find('./ISSN[@IssnType="Print"]')) is not None:
                lookup_issns.append(print_issn_element.text)
            if (title_element := journal_element.find('./Title')) is not None:
                if title_text := title_element.text and title_element.text.strip():
                    record.venue_name = title_text

        from pub import IssnlLookup
        for lookup_issn in lookup_issns:
            if lookup := IssnlLookup.query.get(lookup_issn):
                # record.journal_id = lookup.journal_id
                record.journal_issn_l = lookup.issn_l
                break

        if record.journal_issn_l and (journal := Journal.query.get(record.journal_issn_l)):
            record.publisher = journal.publisher
