import logging
import re
import unicodedata
from datetime import timedelta, datetime

from lxml import etree
from lxml.etree import tostring
from sqlalchemy import insert, inspect
from sqlalchemy.dialects.postgresql import insert

from app import db
from recordthresher.pubmed import PubmedWork, PubmedReference, PubmedAuthor, \
    PubmedAffiliation, PubmedMesh

COMMIT_CHUNK_SIZE = 100


def make_logger():
    # Create a logger object
    logger = logging.getLogger(__name__)

    # Set the logging level (choose from DEBUG, INFO, WARNING, ERROR, CRITICAL)
    logger.setLevel(logging.INFO)

    # Create a console handler to output log messages to the console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Create a logging format
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Add the format to the console handler
    console_handler.setFormatter(log_format)

    # Add the console handler to the logger
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger


LOGGER = make_logger()


def remove_control_characters(s):
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


def get_last_successful_pubmed_batch_start():
    query = "select coalesce(max(started), '1970-01-01') as max_started from recordthresher.pubmed_parse_batch where was_successful;"
    result = db.session.execute(query).fetchone()
    return result[0]


def delete_from_record_queue(last_successful_batch_start):
    query = '''delete from recordthresher.pubmed_record_queue where pmid in (
  select pmid from recordthresher.pubmed_works 
  where created > :last_successful)'''
    db.session.execute(query,
                       {'last_successful': last_successful_batch_start})
    db.session.commit()


def start_batch():
    query = 'insert into recordthresher.pubmed_parse_batch (started) values (now()) returning id;'
    result = db.session.execute(query).fetchone()
    return int(result[0])


def get_raw_records(last_successful_batch_start):
    query = 'SELECT * FROM recordthresher.pubmed_raw WHERE created > :last_successful'
    return db.session.execute(query,
                              {
                                  'last_successful': last_successful_batch_start}).fetchall()


def get_raw_record(pmid):
    query = 'SELECT * FROM recordthresher.pubmed_raw WHERE pmid = :pmid'
    return db.session.execute(query, {'pmid': pmid}).fetchone()


def safe_article_xml(article_xml):
    return re.sub(r'[\x00-\x08\x0B-\x1F\x7F-\x9F]', '',
                  article_xml.replace('\u0000', ''))


def make_tree(record: dict):
    return etree.fromstring(safe_article_xml(record['pubmed_article_xml']))


def model_to_dict(model):
    inspector = inspect(model.__class__)
    d = {}
    for col in inspector.columns:
        d[col.name] = getattr(model, col.name)
    return d


def safe_get_first_xpath(tree: etree.Element, xpath, default=None):
    if result := tree.xpath(xpath):
        return result[0]
    return default


def store_pubmed_work(record: dict, tree=None):
    if tree is None:
        tree = make_tree(record)
    work = PubmedWork()
    work.pubmed_article_xml = safe_article_xml(record['pubmed_article_xml'])
    work.issn = safe_get_first_xpath(tree, '//ISSNLinking/text()')
    work.article_title = safe_get_first_xpath(tree, '//ArticleTitle/text()')
    work.year = safe_get_first_xpath(tree, '//PubDate/Year/text()')
    abstract_texts = tree.xpath('//Abstract/AbstractText')
    work.abstract = '\n'.join(''.join(tag.itertext()) for tag in abstract_texts) if abstract_texts else None
    work.created = datetime.now()
    work.doi = record['doi']
    work.pmid = record['pmid']
    work.pmcid = record['pmcid']
    db.session.add(work)


def store_pubmed_works(records: list):
    for i, record in enumerate(records):
        store_pubmed_work(record)
        if i % COMMIT_CHUNK_SIZE == 0:
            db.session.commit()
            LOGGER.info(f'Stored {i + 1}/{len(records)} works')
    db.session.commit()


def store_pubmed_work_references(record: dict, tree=None):
    if tree is None:
        tree = make_tree(record)
    references = tree.xpath('//ReferenceList/Reference')
    for raw_ref in references:
        ref = PubmedReference()
        ref.created = datetime.now()
        ref.pmid = record['pmid']
        ref.reference = tostring(raw_ref).decode()
        ref.reference_number = int(
            raw_ref.attrib.get('RecordthresherReferenceNo', 1))
        ref.doi = record['doi']
        ref.citation = safe_get_first_xpath(raw_ref, './Citation/text()')
        ref.pmid_referenced = safe_get_first_xpath(tree, '//ArticleId/text()')
        db.session.add(ref)


def store_pubmed_references(records: list):
    for i, record in enumerate(records):
        store_pubmed_work_references(record)
        if i % COMMIT_CHUNK_SIZE == 0:
            db.session.commit()
            LOGGER.info(f'Stored references for {i + 1}/{len(records)} works')
    db.session.commit()


def store_pubmed_authors_and_affiliations(records: list):
    for i, record in enumerate(records):
        store_pubmed_work_authors_and_affiliations(record)
        if i % COMMIT_CHUNK_SIZE == 0:
            db.session.commit()
            LOGGER.info(
                f'Stored authors and references for {i + 1}/{len(records)} works')
    db.session.commit()


def store_pubmed_work_authors_and_affiliations(record: dict, tree=None):
    if tree is None:
        tree = make_tree(record)
    authors = tree.xpath('//AuthorList/Author')
    for raw_author in authors:
        author = PubmedAuthor()
        author.created = datetime.now()
        author.pmid = record['pmid']
        author.doi = record['doi']
        author.author_order = int(
            raw_author.attrib.get('RecordthresherAuthorNo', 1))
        author.family = safe_get_first_xpath(raw_author, './LastName/text()')
        author.given = safe_get_first_xpath(raw_author, './ForeName/text()')
        author.initials = safe_get_first_xpath(raw_author, './Initials/text()')
        author.orcid = safe_get_first_xpath(raw_author,
                                            './Identifier[@Source="ORCID"]/text()')
        stmnt = insert(PubmedAuthor).values(
            **model_to_dict(author)).on_conflict_do_nothing()
        db.session.execute(stmnt)

        affiliations = raw_author.xpath('./AffiliationInfo/Affiliation')
        for raw_aff in affiliations:
            affiliation = PubmedAffiliation()
            affiliation.created = datetime.now()
            affiliation.affiliation = raw_aff.text
            affiliation.author_string = tostring(raw_author).decode()
            affiliation.pmid = author.pmid
            affiliation.affiliation_number = int(
                raw_aff.attrib.get('RecordthresherAuthorAffiliationNo', 1))
            affiliation.author_order = author.author_order
            stmnt = insert(PubmedAffiliation).values(
                **model_to_dict(affiliation)).on_conflict_do_nothing()
            db.session.execute(stmnt)


def store_pubmed_work_mesh(record: dict, tree=None):
    if tree is None:
        tree = make_tree(record)
    raw_meshes = tree.xpath('//MeshHeadingList/MeshHeading')
    for raw_mesh in raw_meshes:
        mesh = PubmedMesh()
        mesh.created = datetime.now()
        mesh.pmid = record['pmid']
        mesh.qualifier_ui = safe_get_first_xpath(raw_mesh,
                                                 './QualifierName/@UI')
        mesh.qualifier_name = safe_get_first_xpath(raw_mesh,
                                                   './QualifierName/text()')
        mesh.descriptor_ui = safe_get_first_xpath(raw_mesh,
                                                  './DescriptorName/@UI')
        mesh.descriptor_name = safe_get_first_xpath(raw_mesh,
                                                    './DescriptorName/text()')
        mesh.is_major_topic = safe_get_first_xpath(raw_mesh,
                                                   './QualifierName/@MajorTopicYN') == 'Y'
        db.session.add(mesh)


def store_pubmed_mesh(records: list):
    for i, record in enumerate(records):
        store_pubmed_work_mesh(record)
        if i % 100 == 0:
            db.session.commit()
            LOGGER.info(f'Stored meshes for {i + 1}/{len(records)} works')
    db.session.commit()


def pre_delete(table, last_successful_batch):
    db.session.execute(f'''delete from recordthresher.{table} where pmid in (
    	select distinct pmid from recordthresher.pubmed_raw 
        where created > :last_successful)''',
                       {'last_successful': last_successful_batch})
    db.session.commit()


def mark_batch_completed(batch_id, last_successful_batch):
    query = '''update recordthresher.pubmed_parse_batch 
                set
                    finished = now(), 
                    was_successful = true, 
                    articles_updated = (
                        select count(*) from recordthresher.pubmed_works 
                        where created > :last_successful_batch
                    )
                where id = :batch_id'''

    db.session.execute(query, {'batch_id': batch_id,
                               'last_successful_batch': last_successful_batch})
    db.session.commit()


def enqueue_to_record_queue(last_successful_batch):
    query = '''insert into recordthresher.pubmed_record_queue (pmid) (
  select pmid from recordthresher.pubmed_works 
  where created > :last_successful_batch
) on conflict do nothing;'''
    db.session.execute(query, {'last_successful_batch': last_successful_batch})
    db.session.commit()


if __name__ == '__main__':
    last_successful_batch = get_last_successful_pubmed_batch_start() - timedelta(
        hours=24)
    LOGGER.info(f'Last successful batch: {last_successful_batch}')
    LOGGER.info(f'Deleting from record queue')
    delete_from_record_queue(last_successful_batch)
    LOGGER.info(f'Finished deleting from record queue')
    batch_id = start_batch()
    raw_records = [dict(row) for row in get_raw_records(last_successful_batch)]
    pre_delete('pubmed_works', last_successful_batch)
    LOGGER.info('Storing works')
    store_pubmed_works(raw_records)
    LOGGER.info('Finished storing works')
    pre_delete('pubmed_reference', last_successful_batch)
    LOGGER.info('Storing references')
    store_pubmed_references(raw_records)
    LOGGER.info('Stored references')
    pre_delete('pubmed_author', last_successful_batch)
    pre_delete('pubmed_affiliation', last_successful_batch)
    LOGGER.info('Storing authors and affiliations')
    store_pubmed_authors_and_affiliations(raw_records)
    LOGGER.info('Finished storing authors and affiliations')
    pre_delete('pubmed_mesh', last_successful_batch)
    LOGGER.info('Storing meshes')
    store_pubmed_mesh(raw_records)
    LOGGER.info('Finished storing meshes')
    LOGGER.info('Marking batch completed')
    mark_batch_completed(batch_id, last_successful_batch)
    LOGGER.info('Finished marking batch completed')
    LOGGER.info('Enqueueing batch to pubmed_record_queue')
    enqueue_to_record_queue(last_successful_batch)
    LOGGER.info('Finished enqueueing batch to pubmed_record_queue')
    LOGGER.info('Done.')
