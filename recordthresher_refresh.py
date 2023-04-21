import re
import time
import traceback
from datetime import datetime
from queue import Queue, Empty
from threading import Thread, Lock

import requests
from sqlalchemy import func, text
from sqlalchemy.exc import NoResultFound
from tenacity import retry, stop_after_attempt, wait_exponential

from app import app, db, logger
import endpoint  # magic
from pub import Pub

PROCESSED_LOCK = Lock()
PROCESSED_COUNT = 0

START = datetime.now()

SEEN_DOIS = set()
SEEN_LOCK = Lock()


def doi_seen(doi):
    with SEEN_LOCK:
        return doi in SEEN_DOIS


def print_openalex_error(retry_state):
    if retry_state.outcome.failed:
        print(
            f'[!] Error making OpenAlex API call (attempt #{retry_state.attempt_number}): {retry_state.outcome.exception()}')


@retry(stop=stop_after_attempt(10),
       wait=wait_exponential(multiplier=1, min=4, max=256),
       retry_error_callback=print_openalex_error)
def get_openalex_json(url, params):
    r = requests.get(url, params=params,
                     verify=False)
    r.raise_for_status()
    return r.json()


def put_dois_api(q: Queue):
    global SEEN_DOIS
    global SEEN_LOCK
    while True:
        try:
            j = get_openalex_json('https://api.openalex.org/works',
                                  params={'sample': '25',
                                          'mailto': 'nolanmccafferty@gmail.com', })
            for work in j["results"]:
                if doi_seen(work['doi']):
                    print(f'Seen DOI already: {work["doi"]}')
                    continue
                try:
                    if not isinstance(work['doi'], str):
                        continue
                    doi = re.findall(r'doi.org/(.*?)$', work['doi'])
                    if not doi:
                        continue
                    pub = Pub.query.filter_by(id=doi[0]).one()
                    q.put(pub)
                    with SEEN_LOCK:
                        SEEN_DOIS.add(work["doi"])
                except NoResultFound:
                    continue
        except Exception as e:
            print(f'[!] Error enqueuing DOIs: {e}')
            print(traceback.format_exc())
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


def process_pubs_loop(q: Queue):
    global PROCESSED_COUNT
    while True:
        pub = None
        try:
            pub = q.get(timeout=60 * 5)
            pub.create_or_update_recordthresher_record()
            db.session.commit()
            with PROCESSED_LOCK:
                PROCESSED_COUNT += 1
        except Empty:
            break
        except Exception:
            if pub:
                print(f'[!] Error updating recordthresher record: {pub.doi}')
            print(traceback.format_exc())
    print('Exiting process pubs loop')


def print_stats(q: Queue = None):
    while True:
        now = datetime.now()
        hrs_running = (now - START).total_seconds() / (60 * 60)
        rate_per_hr = round(PROCESSED_COUNT / hrs_running, 2)
        msg = f'[*] Processed count: {PROCESSED_COUNT} | Rate: {rate_per_hr}/hr | Hrs running: {round(hrs_running, 2)}'
        if q:
            msg += f' | Queue size: {q.qsize()}'
        logger.info(msg)
        time.sleep(5)


# def main():
#     n_threads = int(os.getenv('RECORDTHRESHER_REFRESH_THREADS', 1))
#     q = Queue(maxsize=n_threads*2 + 10)
#     print(f'[*] Starting recordthresher refresh with {n_threads} threads')
#     Thread(target=print_stats, args=(q, ), daemon=True).start()
#     with app.app_context():
#         for _ in range(round(n_threads / 25)):
#             Thread(target=put_dois_api, args=(q,)).start()
#         threads = []
#         for _ in range(n_threads):
#             t = Thread(target=process_pubs_loop, args=(q,))
#             t.start()
#             threads.append(t)
#         for t in threads:
#             t.join()


def refresh_api():
    Thread(target=print_stats).start()
    global PROCESSED_COUNT
    global SEEN_DOIS
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
                        print(f'[!] Seen DOI - {doi}')
                        continue
                    SEEN_DOIS.add(doi)
                    pub = Pub.query.filter_by(id=doi).one()
                    pub.create_or_update_recordthresher_record()
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


def refresh_sql():
    Thread(target=print_stats).start()
    global PROCESSED_COUNT
    global SEEN_DOIS
    query = """SELECT pub.*
                       FROM recordthresher.record AS record TABLESAMPLE BERNOULLI (0.001)
                            JOIN pub ON record.id = pub.recordthresher_id
                       WHERE record.authors::text LIKE '%"affiliation": []%'
                            AND record.updated < '2023-04-14 00:00:00'
                       LIMIT 1;"""
    with app.app_context():
        while True:
            processed = False
            try:
                r = db.session.execute(text(query)).first()
                if r is None:
                    continue
                elif str(r.doi) in SEEN_DOIS:
                    print(f'[!] Seen DOI - {r.doi}')
                    continue
                SEEN_DOIS.add(str(r.doi))
                mapping = dict(r._mapping).copy()
                del mapping['doi']
                pub = Pub(**mapping)
                if pub.create_or_update_recordthresher_record():
                    db.session.commit()
                processed = True
            except Exception as e:
                logger.exception(f'[!] Error updating record: {r.doi} - {e}')
                logger.exception(traceback.format_exc())
            finally:
                if processed:
                    PROCESSED_COUNT += 1


if __name__ == '__main__':
    refresh_sql()
