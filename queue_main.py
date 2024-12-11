import datetime
import os
from subprocess import call
from time import sleep
from time import time

import heroku3

from app import HEROKU_APP_NAME
from app import db
from app import logger
from util import elapsed
from util import get_sql_answer
from util import get_sql_answers
from util import run_sql
from util import safe_commit


class DbQueue(object):

    def __init__(self, **kwargs):
        self.parsed_vars = {}
        super(DbQueue, self).__init__(**kwargs)

    def monitor_till_done(self, job_type):
        logger.info("collecting data. will have some stats soon...")
        logger.info("\n\n")

        num_total = self.number_total_on_queue(job_type)
        print("num_total", num_total)
        num_unfinished = self.number_unfinished(job_type)
        print("num_unfinished", num_unfinished)

        loop_thresholds = {"short": 30, "long": 10*60, "medium": 60}
        loop_unfinished = {"short": num_unfinished, "long": num_unfinished}
        loop_start_time = {"short": time(), "long": time()}

        # print_idle_dynos(job_type)

        while all(loop_unfinished.values()):
            for loop in ["short", "long"]:
                if elapsed(loop_start_time[loop]) > loop_thresholds[loop]:
                    if loop in ["short", "long"]:
                        num_unfinished_now = self.number_unfinished(job_type)
                        num_finished_this_loop = loop_unfinished[loop] - num_unfinished_now
                        loop_unfinished[loop] = num_unfinished_now
                        if loop=="long":
                            logger.info("\n****"),
                        logger.info("   {} finished in the last {} seconds, {} of {} are now finished ({}%).  ".format(
                            num_finished_this_loop, loop_thresholds[loop],
                            num_total - num_unfinished_now,
                            num_total,
                            int(100*float(num_total - num_unfinished_now)/num_total)
                        )),  # comma so the next part will stay on the same line
                        if num_finished_this_loop:
                            minutes_left = float(num_unfinished_now) / num_finished_this_loop * loop_thresholds[loop] / 60
                            logger.info("{} estimate: done in {} mins, which is {} hours".format(
                                loop, round(minutes_left, 1), round(minutes_left/60, 1)))
                        else:
                            print()
                        loop_start_time[loop] = time()
                        # print_idle_dynos(job_type)
            print(".")
            sleep(3)
        logger.info("everything is done.  turning off all the dynos")
        self.scale_dyno(0, job_type)


    def number_total_on_queue(self, job_type):
        num = get_sql_answer(db, "select count(*) from {}".format(self.table_name(job_type)))
        return num

    def number_waiting_on_queue(self, job_type):
        num = get_sql_answer(db, "select count(*) from {} where started is null".format(self.table_name(job_type)))
        return num

    def number_unfinished(self, job_type):
        num = get_sql_answer(db, "select count(*) from {} where finished is null".format(self.table_name(job_type)))
        return num

    def print_status(self, job_type):
        num_dois = self.number_total_on_queue(job_type)
        num_waiting = self.number_waiting_on_queue(job_type)
        if num_dois:
            logger.info("There are {} dois in the queue, of which {} ({}%) are waiting to run".format(
                num_dois, num_waiting, int(100*float(num_waiting)/num_dois)))

    def kick(self, job_type):
        q = """update {table_name} set started=null, finished=null
              where finished is null""".format(
              table_name=self.table_name(job_type))
        run_sql(db, q)
        self.print_status(job_type)

    def reset_enqueued(self, job_type):
        q = "update {} set started=null, finished=null".format(self.table_name(job_type))
        run_sql(db, q)

    def num_dynos(self, job_type):
        heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
        num_dynos = 0
        try:
            dynos = heroku_conn.apps()[HEROKU_APP_NAME].dynos()[self.process_name(job_type)]
            num_dynos = len(dynos)
        except (KeyError, TypeError) as e:
            pass
        return num_dynos

    def print_idle_dynos(self, job_type):
        heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
        app = heroku_conn.apps()[HEROKU_APP_NAME]
        running_dynos = []
        try:
            running_dynos = [dyno for dyno in app.dynos() if dyno.name.startswith(self.process_name(job_type))]
        except (KeyError, TypeError) as e:
            pass

        dynos_still_working = get_sql_answers(db, "select dyno from {} where started is not null and finished is null".format(self.table_name(job_type)))
        dynos_still_working_names = [n for n in dynos_still_working]

        logger.info("dynos still running: {}".format([d.name for d in running_dynos if d.name in dynos_still_working_names]))
        # logger.info(u"dynos stopped:", [d.name for d in running_dynos if d.name not in dynos_still_working_names])
        # kill_list = [d.kill() for d in running_dynos if d.name not in dynos_still_working_names]

    def scale_dyno(self, n, job_type):
        logger.info("starting with {} dynos".format(self.num_dynos(job_type)))
        logger.info("setting to {} dynos".format(n))
        heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
        app = heroku_conn.apps()[HEROKU_APP_NAME]
        app.process_formation()[self.process_name(job_type)].scale(n)

        logger.info("sleeping for 2 seconds while it kicks in")
        sleep(2)
        logger.info("verifying: now at {} dynos".format(self.num_dynos(job_type)))



    def print_logs(self, job_type):
        command = "heroku logs -t --dyno={}".format(self.process_name(job_type))
        call(command, shell=True)



    def update_fn(self, cls, method_name, objects, index=1, kwargs_map=None):

        # we are in a fork!  dispose of our engine.
        # will get a new one automatically
        # if is pooling, need to do .dispose() instead
        db.engine.dispose()

        start = time()
        num_obj_rows = len(objects)

        # logger.info(u"{pid} {repr}.{method_name}() got {num_obj_rows} objects in {elapsed} seconds".format(
        #     pid=os.getpid(),
        #     repr=cls.__name__,
        #     method_name=method_name,
        #     num_obj_rows=num_obj_rows,
        #     elapsed=elapsed(start)
        # ))

        for count, obj in enumerate(objects):
            start_time = time()

            if obj is None:
                return None

            method_to_run = getattr(obj, method_name)

            # logger.info(u"***")
            logger.info("*** #{count} starting {repr}.{method_name}() method".format(
                count=count + (num_obj_rows*index),
                repr=obj,
                method_name=method_name
            ))

            if kwargs_map:
                method_kwargs = kwargs_map.get(obj.id, {})
                method_to_run(**method_kwargs)

            logger.info("finished {repr}.{method_name}(). took {elapsed} seconds".format(
                repr=obj,
                method_name=method_name,
                elapsed=elapsed(start_time, 4)
            ))

            # for handling the queue
            if not (method_name == "update" and obj.__class__.__name__ == "Pub"):
                obj.finished = datetime.datetime.utcnow().isoformat()
            # db.session.merge(obj)


        start_time = time()
        commit_success = safe_commit(db)
        if not commit_success:
            logger.info("COMMIT fail")
        logger.info("commit took {} seconds".format(elapsed(start_time, 2)))
        db.session.remove()  # close connection nicely
        return None  # important for if we use this on RQ

    def run(self, parsed_args, job_type):
        start = time()

        try:
            self.worker_run(**vars(parsed_args))
        except Exception as e:
            logger.fatal('worker_run died with error:', exc_info=True)


        logger.info("finished update in {} seconds".format(elapsed(start)))
        # resp = None
        # if job_type in ["normal"]:
        #     my_location = Page.query.get(parsed_args.id)
        #     resp = my_location.__dict__
        #     pprint(resp)

        print("done")
        return


    def print_update(self, new_loop_start_time, chunk, limit, start_time, index):
        num_items = limit  #let's say have to do the full limit
        num_jobs_remaining = num_items - (index * chunk)
        try:

            jobs_per_hour_this_chunk = chunk / float(elapsed(new_loop_start_time) / 3600)
            predicted_mins_to_finish = round(
                (num_jobs_remaining / float(jobs_per_hour_this_chunk)) * 60,
                1
            )
            logger.info("\n\nWe're doing {} jobs per hour. At this rate, if we had to do everything up to limit, done in {}min".format(
                int(jobs_per_hour_this_chunk),
                predicted_mins_to_finish
            ))
            logger.info("\t{} seconds this loop, {} chunks in {} seconds, {} seconds/chunk average\n".format(
                elapsed(new_loop_start_time),
                index,
                elapsed(start_time),
                round(elapsed(start_time)/float(index), 1)
            ))
        except ZeroDivisionError:
            # logger.info(u"not printing status because divide by zero")
            logger.info(".")


    def run_right_thing(self, parsed_args, job_type):
        if parsed_args.dynos != None:  # to tell the difference from setting to 0
            self.scale_dyno(parsed_args.dynos, job_type)

        if parsed_args.reset:
            self.reset_enqueued(job_type)

        if parsed_args.status:
            self.print_status(job_type)

        if parsed_args.monitor:
            self.monitor_till_done(job_type)
            self.scale_dyno(0, job_type)

        if parsed_args.logs:
            self.print_logs(job_type)

        if parsed_args.kick:
            self.kick(job_type)

        if parsed_args.id or parsed_args.doi or parsed_args.run:
            self.run(parsed_args, job_type)


    ## these are overwritten by main class

    def table_name(self, job_type):
        pass

    def process_name(self, job_type):
        pass

    def worker_run(self, **kwargs):
        pass
