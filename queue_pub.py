import argparse
import logging
import os
import random
from time import sleep
from time import time

from redis.client import Redis
from sqlalchemy import orm
from sqlalchemy import text

from app import db, oa_db_engine
from app import logger
from endpoint import Endpoint  # magic
from pub import Pub
from queue_main import DbQueue
from util import elapsed, enqueue_slow_queue, enqueue_unpaywall_refresh
from util import normalize_doi
from util import run_sql


class DbQueuePub(DbQueue):
    def table_name(self, job_type):
        table_name = "pub"
        return table_name

    def process_name(self, job_type):
        if self.parsed_vars:
            process_name = self.parsed_vars.get("method")
        return process_name

    def worker_run(self, **kwargs):
        dois = kwargs.get("doi", [])
        chunk = kwargs.get("chunk", 100)
        limit = kwargs.get("limit", 10)
        run_class = Pub
        run_method = kwargs.get("method")

        if dois:
            limit = len(dois)
            queue_table = None
        elif run_method == "refresh":
            queue_table = "pub_refresh_queue"
            if not limit:
                limit = 1000
            text_query_pattern = """
                with refresh_queue as (
                    select id
                    from {queue_table}
                    where started is null
                    order by
                        priority desc,
                        finished nulls first,
                        started,
                        rand
                    limit {chunk}
                    for update skip locked
                )
                update {queue_table} queue_rows_to_update
                set started = now()
                from refresh_queue
                where refresh_queue.id = queue_rows_to_update.id
                returning refresh_queue.id;"""
            text_query = text_query_pattern.format(
                chunk=chunk,
                queue_table=queue_table
            )
        else:
            queue_table = "pub_queue"
            if not limit:
                limit = 1000
            text_query_pattern = """WITH update_pub_queue AS (
                       SELECT id
                       FROM   {queue_table}
                       WHERE  started is null
                       order by finished asc
                       nulls first
                   LIMIT  {chunk}
                   FOR UPDATE SKIP LOCKED
                   )
                UPDATE {queue_table} queue_rows_to_update
                SET    started=now()
                FROM   update_pub_queue
                WHERE update_pub_queue.id = queue_rows_to_update.id
                RETURNING update_pub_queue.id;"""
            text_query = text_query_pattern.format(
                limit=limit,
                chunk=chunk,
                queue_table=queue_table
            )

        index = 0
        start_time = time()
        oa_db_conn = oa_db_engine.connect()
        oa_redis_conn = Redis.from_url(os.environ["REDIS_DO_URL"])

        while True:
            new_loop_start_time = time()
            if dois:
                normalized_dois = [normalize_doi(doi) for doi in dois]
                objects = run_class.query.filter(
                    run_class.id.in_(normalized_dois)).all()
                if not objects:
                    logger.info(f"No publications found for DOIs: {dois}")
                    return
            else:
                logger.info("looking for new jobs")

                job_time = time()
                row_list = db.engine.execute(text(text_query).execution_options(
                    autocommit=True)).fetchall()
                object_ids = [row[0] for row in row_list]
                logger.info(
                    "got ids, took {} seconds".format(elapsed(job_time)))

                job_time = time()
                q = db.session.query(Pub).options(orm.undefer('*')).filter(
                    Pub.id.in_(object_ids))
                objects = q.all()
                logger.info(
                    "got pub objects in {} seconds".format(elapsed(job_time)))

                random.shuffle(objects)

            if not objects:
                if not dois:  # Only sleep and continue if we're processing from queue
                    sleep(5)
                    continue
                return

            object_ids = [obj.id for obj in objects]
            self.update_fn(run_class, run_method, objects, index=index)
            enqueue_unpaywall_refresh(object_ids, oa_db_conn, oa_redis_conn)
            logger.info(
                f'Enqueued {len(object_ids)} works to be updated in unpaywall_recordthresher_fields')

            if queue_table:
                object_ids_str = ",".join(
                    ["'{}'".format(id.replace("'", "''")) for id in object_ids])
                object_ids_str = object_ids_str.replace("%",
                                                        "%%")  # sql escaping
                sql_command = "update {queue_table} set finished=now(), started=null where id in ({ids})".format(
                    queue_table=queue_table, ids=object_ids_str)
                run_sql(db, sql_command)

            index += 1
            if dois:
                return
            else:
                self.print_update(new_loop_start_time, chunk, limit, start_time,
                                  index)


if __name__ == "__main__":
    if os.getenv('OADOI_LOG_SQL'):
        logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
        db.session.configure()

    parser = argparse.ArgumentParser(description="Run stuff.")
    parser.add_argument('--id', nargs="?", type=str, help="id of the one thing you want to update (case sensitive)")
    parser.add_argument('--doi', nargs="+", type=str,
                        help="list of DOIs to update (case sensitive)")
    parser.add_argument('--method', nargs="?", type=str, default="update",
                        help="method name to run")

    parser.add_argument('--reset', default=False, action='store_true',
                        help="do you want to just reset?")
    parser.add_argument('--run', default=False, action='store_true',
                        help="to run the queue")
    parser.add_argument('--status', default=False, action='store_true',
                        help="to logger.info(the status")
    parser.add_argument('--dynos', default=None, type=int,
                        help="scale to this many dynos")
    parser.add_argument('--logs', default=False, action='store_true',
                        help="logger.info(out logs")
    parser.add_argument('--monitor', default=False, action='store_true',
                        help="monitor till done, then turn off dynos")
    parser.add_argument('--kick', default=False, action='store_true',
                        help="put started but unfinished dois back to unstarted so they are retried")
    parser.add_argument('--limit', "-l", nargs="?", type=int,
                        help="how many jobs to do")
    parser.add_argument('--chunk', "-ch", nargs="?", default=500, type=int,
                        help="how many to take off db at once")

    parsed_args = parser.parse_args()

    job_type = "normal"  # should be an object attribute
    my_queue = DbQueuePub()
    my_queue.parsed_vars = vars(parsed_args)
    my_queue.run_right_thing(parsed_args, job_type)