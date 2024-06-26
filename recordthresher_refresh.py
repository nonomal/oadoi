import re
import time
import traceback
from argparse import ArgumentParser
from datetime import datetime
from queue import Queue, Empty
from threading import Thread, Lock
from typing import List

from pyalex import Works, config
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound, PendingRollbackError

from app import app, db, logger
from pub import Pub
from util import normalize_doi, get_openalex_json, enqueue_slow_queue
from endpoint import Endpoint  # magic

from app import oa_db_engine

PROCESSED_LOCK = Lock()
PROCESSED_COUNT = 0

START = datetime.now()

SEEN_DOIS = set()
SEEN_LOCK = Lock()

DB_SESSION_LOCK = Lock()

DUPE_COUNT = 0

ENQUEUE_SLOW_QUEUE_CHUNK_SIZE = 100


def db_commit():
    with DB_SESSION_LOCK:
        db.session.commit()


def doi_seen(doi):
    global SEEN_DOIS
    global SEEN_LOCK
    with SEEN_LOCK:
        return doi in SEEN_DOIS


def add_seen_doi(doi):
    global SEEN_DOIS
    global SEEN_LOCK
    with SEEN_LOCK:
        SEEN_DOIS.add(doi)


def put_dois_api(q: Queue):
    global SEEN_DOIS
    global SEEN_LOCK
    while True:
        try:
            j = get_openalex_json('https://api.openalex.org/works',
                                  params={
                                      'filter': 'authorships.institutions.id:null,type:journal-article,has_doi:true',
                                      'per-page': '25',
                                      'sample': '25',
                                      'mailto': 'nolanmccafferty@gmail.com', })
            for work in j["results"]:
                if not isinstance(work['doi'], str):
                    continue
                doi = re.findall(r'doi.org/(.*?)$', work['doi'])
                if not doi:
                    continue
                doi = doi[0]
                if doi_seen(doi):
                    logger.info(f'[!] Seen DOI already: {doi}')
                    continue
                try:
                    pub = Pub.query.filter_by(id=doi).one()
                    q.put(pub)
                    add_seen_doi(doi)
                except NoResultFound:
                    continue
        except Exception as e:
            logger.exception(f'[!] Error enqueuing DOIs: {e}')
            logger.exception(traceback.format_exc())
            break


def put_dois_db(q: Queue):
    page_size = 1000
    offset = 0
    results = True
    while results:
        results = Pub.query.limit(page_size).offset(offset).all()
        for result in results:
            q.put(result)
        offset += page_size


def print_stats(q: Queue = None):
    while True:
        now = datetime.now()
        hrs_running = (now - START).total_seconds() / (60 * 60)
        rate_per_hr = round(PROCESSED_COUNT / hrs_running, 2)
        msg = f'[*] Processed count: {PROCESSED_COUNT} | Encountered dupe count: {DUPE_COUNT} | Rate: {rate_per_hr}/hr | Hrs running: {round(hrs_running, 2)}'
        if q:
            msg += f' | Queue size: {q.qsize()}'
        logger.info(msg)
        time.sleep(5)


def refresh_api():
    Thread(target=print_stats).start()
    global PROCESSED_COUNT
    global SEEN_DOIS
    global DUPE_COUNT
    with app.app_context():
        while True:
            processed = False
            j = get_openalex_json('https://api.openalex.org/works',
                                  params={
                                      'filter': 'authorships.institutions.id:null,type:journal-article,has_doi:true',
                                      'per-page': '25',
                                      'sample': '25',
                                      'mailto': 'nolanmccafferty@gmail.com', })
            for work in j["results"]:
                doi = None
                try:
                    if not isinstance(work['doi'], str):
                        continue
                    doi = re.findall(r'doi.org/(.*?)$', work['doi'])
                    if not doi:
                        continue
                    doi = doi[0]
                    if doi in SEEN_DOIS:
                        logger.info(f'[!] Seen DOI - {doi}')
                        DUPE_COUNT += 1
                        continue
                    SEEN_DOIS.add(doi)
                    pub = Pub.query.filter_by(id=doi).one()
                    if pub.create_or_update_recordthresher_record():
                        db.session.commit()
                    processed = True
                except NoResultFound:
                    continue
                except Exception as e:
                    if doi:
                        logger.info(f'[!] Error updating record: {doi}')
                    logger.exception(e)
                finally:
                    if processed:
                        PROCESSED_COUNT += 1


def enqueue_slow_queue_worker(q: Queue):
    dois = []
    with oa_db_engine.connect() as oa_db_conn:
        while True:
            try:
                doi = q.get(timeout=5 * 60)
                dois.append(doi)
                if len(dois) >= ENQUEUE_SLOW_QUEUE_CHUNK_SIZE:
                    enqueue_slow_queue(list(set(dois)), oa_db_conn)
                    dois = []
            except Empty:
                return


def refresh_sql(slow_queue_q: Queue, chunk_size=10):
    global PROCESSED_COUNT
    query = f'''WITH queue as (
            SELECT * FROM recordthresher.refresh_queue WHERE in_progress = false
            LIMIT {chunk_size}
            FOR UPDATE SKIP LOCKED
            )
            UPDATE recordthresher.refresh_queue enqueued
            SET in_progress = true
            FROM queue WHERE queue.id = enqueued.id
            RETURNING *
            '''
    rows = True
    with app.app_context():
        while rows:
            rows = db.session.execute(text(query)).all()
            for r in rows:
                processed = False
                mapping = dict(r._mapping).copy()
                del mapping['doi']
                pub = Pub(**mapping)
                try:
                    if pub.create_or_update_recordthresher_record():
                        db.session.commit()
                    processed = True
                    slow_queue_q.put(r.id)
                except PendingRollbackError:
                    db.session.rollback()
                    logger.exception('[*] Rolled back transaction')
                except Exception as e:
                    logger.exception(
                        f'[!] Error updating record: {r.id} - {e}')
                    logger.exception(traceback.format_exc())
                finally:
                    id_ = r['id']
                    del_query = "DELETE FROM recordthresher.refresh_queue WHERE id = :id_"
                    db.session.execute(text(del_query).bindparams(id_=id_))
                    if processed:
                        PROCESSED_COUNT += 1


def filter_string_to_dict(oa_filter_str):
    items = oa_filter_str.split(',')
    d = {}
    for item in items:
        k, v = item.split(':', maxsplit=1)
        d[k] = v
    return d


def enqueue_from_api(oa_filters):
    config.email = 'nolanmccafferty@gmail.com'
    for oa_filter in oa_filters:
        print(f'[*] Starting to enqueue using OA filter: {oa_filter}')
        d = filter_string_to_dict(oa_filter)
        pager = iter(Works().filter(**d).paginate(per_page=200, n_max=None))
        i = 0
        while True:
            try:
                page = next(pager)
                dois = tuple({normalize_doi(work['doi'], True) for work in page})
                dois = tuple([doi for doi in dois if doi])
                stmnt = 'INSERT INTO recordthresher.refresh_queue SELECT * FROM pub WHERE id IN :dois ON CONFLICT DO NOTHING;'
                db.session.execute(text(stmnt).bindparams(dois=dois))
                db.session.commit()
                # publisher = page[0]['primary_location']['source'][
                #     'host_organization_name']
                # pub_id = page[0]['primary_location']['source'][
                #     'host_organization']
                print(
                    f'[*] Inserted {200 * (i + 1)} into refresh queue from filter - {oa_filter}')
            except StopIteration:
                break
            except Exception as e:
                print(f'[!] Error fetching page for filter - {oa_filter}')
                print(traceback.format_exc())
            finally:
                i += 1


def enqueue_from_txt(path):
    with open(path) as f:
        contents = f.read()
        dois = list(
            set([line.split('doi.org/')[-1] if line.startswith('http') else line
                 for line in contents.splitlines()]))
        dois = tuple(normalize_doi(doi) for doi in dois)
        stmnt = 'INSERT INTO recordthresher.refresh_queue SELECT * FROM pub WHERE id IN :dois ON CONFLICT(id) DO UPDATE SET in_progress = FALSE;'
        db.session.execute(text(stmnt).bindparams(dois=dois))
        db.session.commit()
    print(f'Enqueued {len(dois)} DOIS from {path}')


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--txt',
                        help='Path to txt file to refresh DOIs (one per line)',
                        type=str)
    parser.add_argument('--n_threads', '-n', help='Number of threads to use',
                        type=int, default=10)
    parser.add_argument('--oa_filters', '-f', action='append',
                        help='OpenAlex filters from which to enqueue works to recordthresher refresh')

    return parser.parse_args()


def split(a, n):
    k, m = divmod(len(a), n)
    return (a[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n))


if __name__ == '__main__':
    args = parse_args()
    if args.txt:
        enqueue_from_txt(args.txt)
    if args.oa_filters:
        threads = []
        # pub_ids = list(set(args.enqueue_pub))
        # base_oa_filter = 'type:journal-article,has_doi:true,has_raw_affiliation_string:false,publication_date:>2015-01-01'
        chunks = split(list(set(args.oa_filters)), args.n_threads)
        for chunk in chunks:
            t = Thread(target=enqueue_from_api, args=(chunk,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    else:
        slow_queue_q = Queue(maxsize=args.n_threads)
        Thread(target=enqueue_slow_queue_worker, args=(slow_queue_q,)).start()
        Thread(target=print_stats, daemon=True).start()
        threads = []
        for _ in range(args.n_threads):
            t = Thread(target=refresh_sql, args=(slow_queue_q,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()
