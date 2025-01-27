import argparse
import concurrent.futures
import logging
import os
import pickle
from datetime import datetime, timedelta
from multiprocessing import current_process
from time import sleep
from time import time
from urllib.parse import urlparse

import redis
from redis import WatchError
from sqlalchemy import orm, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import make_transient

from app import db
from app import logger
from oa_page import publisher_equivalent_endpoint_id
from page import PageNew
from queue_main import DbQueue
from recordthresher.record import RecordthresherParentRecord
from recordthresher.record_maker import PmhRecordMaker
from util import elapsed
from util import safe_commit

from pub import Pub  # magic
import endpoint  # magic
import pmh_record  # magic

def _procs_per_worker():
    return int(os.getenv('GREEN_SCRAPE_PROCS_PER_WORKER', 10))


def _redis_max_connections():
    return 2


_redis_client = None
_redis_init = False


def get_redis_client():
    global _redis_client, _redis_init

    if not _redis_init:
        try:
            _redis_client = redis.from_url(os.environ.get("REDIS_URL"), max_connections=1)
        except Exception as e:
            logger.exception(f'failed creating redis client: {e}')

        _redis_init = True

    return _redis_client


def scrape_pages(pages):
    for page in pages:
        make_transient(page)

    # free up the connection while doing net IO
    db.session.close()
    db.engine.dispose()

    with concurrent.futures.ProcessPoolExecutor(max_workers=_procs_per_worker()) as pool:
        map_results = pool.map(scrape_page, pages, chunksize=1)
        scraped_pages = [p for p in map_results if p]

    logger.info('finished scraping all pages')

    logger.info('preparing update records')
    extant_page_ids = [
        row[0] for row in
        db.session.query(PageNew.id).filter(PageNew.id.in_(
            [p.id for p in scraped_pages]
        )).all()
    ]

    scraped_pages = [db.session.merge(p) for p in scraped_pages if p.id in extant_page_ids]

    for scraped_page in scraped_pages:
        scraped_page.save_first_version_availability()

    return scraped_pages


def scrape_page(page):
    with concurrent.futures.ProcessPoolExecutor(max_workers=1) as pool:
        worker = current_process().name
        try:
            return pool.submit(scrape_page_worker, page).result(timeout=300)
        except concurrent.futures.TimeoutError as e:
            logger.error(f'{worker} timed out')
            for pid, process in pool._processes.items():
                process.terminate()
            pool.shutdown()
            return None
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(f'{worker} exception scraping page {page.id}')
            return None


def scrape_page_worker(page):
    worker = current_process().name
    site_key_stem = redis_key(page, '')

    logger.info('{} started scraping page {} {} {}'.format(worker, page.id, site_key_stem, page))

    total_wait_seconds = 0
    wait_seconds = 5
    while total_wait_seconds < 60:
        if begin_rate_limit(page):
            page.scrape()
            end_rate_limit(page)
            logger.info('{} finished scraping page {} {} {}'.format(worker, page.id, site_key_stem, page))
            return page
        else:
            logger.info('{} not ready to scrape page {} {} {}, waiting'.format(worker, page.id, site_key_stem, page))
            sleep(wait_seconds)
            total_wait_seconds += wait_seconds

    logger.info('{} done waiting to scrape page {} {} {}, giving up'.format(worker, page.id, site_key_stem, page))

    return None


def unpickle(v):
    return pickle.loads(v) if v else None


def redis_key(page, scrape_property):
    domain = urlparse(page.url).netloc
    return 'green-scrape-p3:{}:{}:{}'.format(page.endpoint_id, domain, scrape_property)


def scrape_interval_seconds(page):
    hostname = urlparse(page.url).hostname

    one_sec_hosts = [
        'citeseerx.ist.psu.edu',
        'www.ncbi.nlm.nih.gov',
        'pt.cision.com',
        'doaj.org',
        'hal.archives-ouvertes.fr',
        'figshare.com',
        'arxiv.org',
        'europepmc.org',
        'bibliotheques-specialisees.paris.fr',
        'nbn-resolving.de',
        'osti.gov',
        'zenodo.org',
        'kuleuven.be',
        'edoc.hu-berlin.de',
        'rug.nl',
    ]

    for host in one_sec_hosts:
        if hostname and hostname.endswith(host):
            return 1

    return 10


def begin_rate_limit(page, interval_seconds=None):
    redis_client = get_redis_client()

    if page.endpoint_id == publisher_equivalent_endpoint_id:
        return True

    interval_seconds = interval_seconds or scrape_interval_seconds(page)

    started_key = redis_key(page, 'started')
    finished_key = redis_key(page, 'finished')

    with redis_client.pipeline() as pipe:
        try:
            pipe.watch(started_key)
            pipe.watch(finished_key)

            scrape_started = unpickle(pipe.get(started_key))

            if scrape_started and scrape_started >= datetime.utcnow() - timedelta(seconds=interval_seconds):
                return False

            pipe.multi()
            pipe.set(started_key, pickle.dumps(datetime.utcnow()))
            pipe.set(finished_key, pickle.dumps(None))
            pipe.execute()
            return True
        except WatchError:
            return False


def end_rate_limit(page):
    redis_client = get_redis_client()
    redis_client.set(redis_key(page, 'started'), pickle.dumps(None))
    redis_client.set(redis_key(page, 'finished'), pickle.dumps(datetime.utcnow()))


def merge_and_commit_objects(objects, retry=2):
    try:
        logger.info('starting merge')
        merge_start_time = time()
        [db.session.merge(o) for o in objects]
        logger.info("merge took {} seconds".format(elapsed(merge_start_time, 2)))

        logger.info('starting commit')
        commit_start_time = time()
        db.session.commit()
        logger.info("commit took {} seconds".format(elapsed(commit_start_time, 2)))
    except IntegrityError as e:
        logger.exception(f'integrity error merging objects: {e}')
        db.session.rollback()

        if retry > 0:
            logger.info('retrying merge_and_commit_objects')
            merge_and_commit_objects(objects, retry=retry-1)
        else:
            logger.error('giving up on merge_and_commit_objects')
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        db.session.rollback()
        logger.exception(f'error merging objects: {e}')


class DbQueueGreenOAScrape(DbQueue):
    def table_name(self, job_type):
        return 'page_green_scrape_queue'

    def process_name(self, job_type):
        return 'run_green_oa_scrape'

    def worker_run(self, **kwargs):
        run_class = PageNew

        single_id = kwargs.get("id", None)
        chunk_size = kwargs.get("chunk", 100)
        limit = kwargs.get("limit", None)
        scrape_publisher = kwargs.get("scrape_publisher", False)

        if limit is None:
            limit = float("inf")

        if single_id:
            page = run_class.query.filter(run_class.id == single_id).first()
            page.scrape()
            page.save_first_version_availability()
            db.session.merge(page)

            safe_commit(db) or logger.info("COMMIT fail")

            if recordthresher_record := PmhRecordMaker.make_record(page.pmh_record):
                db.session.merge(recordthresher_record)
                secondary_records = PmhRecordMaker.make_secondary_repository_responses(recordthresher_record)
                for secondary_record in secondary_records:
                    db.session.merge(secondary_record)
                    db.session.merge(
                        RecordthresherParentRecord(
                            record_id=secondary_record.id,
                            parent_record_id=recordthresher_record.id
                        )
                    )

            safe_commit(db) or logger.info("COMMIT fail")
        else:
            index = 0
            num_updated = 0
            start_time = time()

            while num_updated < limit:
                new_loop_start_time = time()

                objects = self.fetch_queue_chunk(chunk_size, scrape_publisher)

                if not objects:
                    logger.info('no queued pages ready. waiting...')
                    sleep(5)
                    continue

                scraped_pages = scrape_pages(objects)

                scraped_ids = [p.id for p in scraped_pages]
                unscraped_ids = [obj.id for obj in objects if obj.id not in scraped_ids]

                logger.info('scraped {} pages and returned {} to the queue'.format(
                    len(scraped_ids), len(unscraped_ids)
                ))

                scraped_batch_text = '''
                    update {queue_table}
                    set finished = now(), started=null
                    where id = any(:ids)'''.format(queue_table=self.table_name(None))

                unscraped_batch_text = '''
                     update {queue_table}
                     set started=null
                     where id = any(:ids)'''.format(queue_table=self.table_name(None))

                scraped_batch_command = text(scraped_batch_text).bindparams(
                    ids=scraped_ids)

                unscraped_batch_command = text(unscraped_batch_text).bindparams(
                    ids=unscraped_ids)

                db.session.execute(scraped_batch_command)
                db.session.execute(unscraped_batch_command)

                commit_start_time = time()
                safe_commit(db) or logger.info("COMMIT fail")
                logger.info("commit took {} seconds".format(elapsed(commit_start_time, 2)))

                logger.info('making recordthresher records')

                recordthresher_records = [PmhRecordMaker.make_record(p.pmh_record) for p in scraped_pages]

                distinct_records = {}
                for recordthresher_record in recordthresher_records:
                    if recordthresher_record:
                        distinct_records[recordthresher_record.id] = recordthresher_record

                if distinct_records:
                    logger.info('saving recordthresher records')
                    merge_and_commit_objects(distinct_records.values())

                    logger.info('making secondary repository records')
                    secondary_records = {}
                    parent_relationships = {}

                    for r in distinct_records.values():
                        r_secondary_records = PmhRecordMaker.make_secondary_repository_responses(r)
                        for r_secondary_record in r_secondary_records:
                            secondary_records[r_secondary_record.id] = r_secondary_record
                            parent_relationships[r_secondary_record.id] = RecordthresherParentRecord(
                                record_id=r_secondary_record.id,
                                parent_record_id=r.id
                            )

                    if secondary_records:
                        logger.info('saving secondary repository records')
                        merge_and_commit_objects(secondary_records.values())

                    if parent_relationships:
                        logger.info('saving parent_relationships')
                        merge_and_commit_objects(parent_relationships.values())

                index += 1
                num_updated += chunk_size
                self.print_update(new_loop_start_time, len(scraped_ids), limit, start_time, index)

    def fetch_queue_chunk(self, chunk_size, scrape_publisher):
        logger.info("looking for new jobs")

        endpoint_filter = "and qt.endpoint_id {} '{}'".format(
            '=' if scrape_publisher else 'is distinct from',
            publisher_equivalent_endpoint_id
        )

        text_query_pattern = """
            with update_chunk as (
                select
                    lru_by_endpoint.id
                    from
                        endpoint e
                        cross join lateral (
                            select qt.*
                            from
                                {queue_table} qt
                                join page_new p using (id)
                            where
                                qt.endpoint_id = e.id
                                and qt.started is null
                                and e.green_scrape
                                {endpoint_filter}
                                and p.pmh_record is null
                            order by qt.finished asc nulls first
                            limit {per_endpoint_limit}
                            for update of qt skip locked
                        ) lru_by_endpoint
                    where
                        finished is null lru_by_endpoint.rand
                    limit {chunk_size}
            )
            update {queue_table} queue_rows_to_update
            set started=now()
            from update_chunk
            where update_chunk.id = queue_rows_to_update.id
            returning update_chunk.id;
        """

        text_query = text_query_pattern.format(
            chunk_size=chunk_size,
            queue_table=self.table_name(None),
            endpoint_filter=endpoint_filter,
            per_endpoint_limit=chunk_size if scrape_publisher else 10
        )

        job_time = time()
        row_list = db.engine.execute(text(text_query).execution_options(autocommit=True)).fetchall()
        object_ids = [row[0] for row in row_list]
        logger.info("got {} ids, took {} seconds".format(len(object_ids), elapsed(job_time)))

        job_time = time()
        q = db.session.query(PageNew).options(
            orm.undefer('*')
        ).filter(PageNew.id.in_(object_ids))

        objects = q.all()
        logger.info("got page_new objects in {} seconds".format(elapsed(job_time)))

        return objects


if __name__ == "__main__":
    if os.getenv('OADOI_LOG_SQL'):
        logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
        db.session.configure()

    parser = argparse.ArgumentParser(description="Run stuff.")
    parser.add_argument('--id', nargs="?", type=str, help="id of the one thing you want to update (case sensitive)")
    parser.add_argument('--doi', nargs="?", type=str, help="id of the one thing you want to update (case insensitive)")

    parser.add_argument('--reset', default=False, action='store_true', help="do you want to just reset?")
    parser.add_argument('--run', default=False, action='store_true', help="to run the queue")
    parser.add_argument('--status', default=False, action='store_true', help="to logger.info(the status")
    parser.add_argument('--dynos', default=None, type=int, help="scale to this many dynos")
    parser.add_argument('--logs', default=False, action='store_true', help="logger.info(out logs")
    parser.add_argument('--monitor', default=False, action='store_true', help="monitor till done, then turn off dynos")
    parser.add_argument('--kick', default=False, action='store_true', help="put started but unfinished dois back to unstarted so they are retried")
    parser.add_argument('--limit', "-l", nargs="?", type=int, help="how many jobs to do")
    parser.add_argument('--chunk', "-ch", nargs="?", default=100, type=int, help="how many to take off db at once")

    parser.add_argument('--scrape-publisher', default=False, action='store_true', help="scrape publisher-equivalent pages")

    parsed_args = parser.parse_args()

    job_type = "normal"  # should be an object attribute
    my_queue = DbQueueGreenOAScrape()
    my_queue.parsed_vars = vars(parsed_args)
    my_queue.run_right_thing(parsed_args, job_type)
