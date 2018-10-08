#!/usr/bin/env python

import argparse
import logging
import os
import sys
# import random
import threading
import pickle
import time
import queue
import uuid
import zmq

from mpi4py import MPI

from ipyparallel.serialize import unpack_apply_message  # pack_apply_message,
from ipyparallel.serialize import serialize_object
# from parsl.executors.mpix import zmq_pipes

RESULT_TAG = 10
TASK_REQUEST_TAG = 11

LOOP_SLOWDOWN = 0.0  # in seconds


class Daimyo(object):
    """ Daimyo (feudal lord) rules over the workers

    1. Asynchronously queue large volume of tasks
    2. Allow for workers to join and leave the union
    3. Detect workers that have failed using heartbeats
    4. Service single and batch requests from workers
    5. Be aware of requests worker resource capacity,
       eg. schedule only jobs that fit into walltime.
    """
    def __init__(self,
                 comm, rank,
                 task_q_url="tcp://127.0.0.1:50097",
                 result_q_url="tcp://127.0.0.1:50098",
                 max_queue_size=10,
                 heartbeat_period=30,
                 uid=None):
        """
        Parameters
        ----------
        worker_url : str
             Worker url on which workers will attempt to connect back
        """
        logger.info("Daimyo started v0.5")
        self.uid = uid

        self.context = zmq.Context()
        self.task_incoming = self.context.socket(zmq.DEALER)
        self.task_incoming.setsockopt(zmq.IDENTITY, b'00100')
        self.task_incoming.connect(task_q_url)

        self.result_outgoing = self.context.socket(zmq.DEALER)
        self.result_outgoing.setsockopt(zmq.IDENTITY, b'00100')
        self.result_outgoing.connect(result_q_url)

        logger.info("Daimyo connected")
        if max_queue_size == 0:
            max_queue_size = comm.size
        self.pending_task_queue = queue.Queue(maxsize=max_queue_size)
        self.pending_result_queue = queue.Queue(maxsize=10 ^ 4)
        self.ready_worker_queue = queue.Queue(maxsize=max_queue_size + 10)

        self.tasks_per_round = 1

        self.heartbeat_period = heartbeat_period
        self.comm = comm
        self.rank = rank

    def heartbeat(self):
        """ Send heartbeat to the incoming task queue
        """
        heartbeat = (0).to_bytes(4, "little")
        r = self.task_incoming.send(heartbeat)
        logger.debug("Return from heartbeat : {}".format(r))

    def recv_result_from_workers(self):
        """ Receives a results from the MPI fabric and send it out via 0mq

        Returns:
        --------
            result: task result from the workers
        """
        info = MPI.Status()
        result = self.comm.recv(source=MPI.ANY_SOURCE, tag=RESULT_TAG, status=info)
        logger.debug("Received result from workers: {}".format(result))
        return result

    def recv_task_request_from_workers(self):
        """ Receives 1 task request from MPI comm

        Returns:
        --------
            worker_rank: worker_rank id
        """
        info = MPI.Status()
        comm.recv(source=MPI.ANY_SOURCE, tag=TASK_REQUEST_TAG, status=info)
        worker_rank = info.Get_source()
        logger.info("Received task request from worker:{}".format(worker_rank))
        return worker_rank

    def pull_tasks(self, kill_event):
        """ Pulls tasks from the incoming tasks 0mq pipe onto the internal
        pending task queue

        Parameters:
        -----------
        kill_event : threading.Event
              Event to let the thread know when it is time to die.
        """
        logger.info("[TASK PULL THREAD] starting")
        poller = zmq.Poller()
        poller.register(self.task_incoming, zmq.POLLIN)
        self.heartbeat()
        last_beat = time.time()
        task_recv_counter = 0

        while not kill_event.is_set():
            time.sleep(LOOP_SLOWDOWN)
            ready_worker_count = self.ready_worker_queue.qsize()
            logger.debug("[TASK_PULL_THREAD] ready worker queue size: {}".format(ready_worker_count))

            if time.time() > last_beat + (float(self.heartbeat_period) / 2):
                self.heartbeat()
                last_beat = time.time()

            if ready_worker_count > 0:

                ready_worker_count = 4
                logger.debug("[TASK_PULL_THREAD] Requesting tasks: {}".format(ready_worker_count))
                msg = ((ready_worker_count).to_bytes(4, "little"))
                self.task_incoming.send(msg)

            # start = time.time()
            socks = dict(poller.poll(1))
            # delta = time.time() - start

            if self.task_incoming in socks and socks[self.task_incoming] == zmq.POLLIN:
                _, pkl_msg = self.task_incoming.recv_multipart()
                tasks = pickle.loads(pkl_msg)
                if tasks == 'STOP':
                    logger.critical("[TASK_PULL_THREAD] Received stop request")
                    kill_event.set()
                    break
                else:
                    logger.debug("[TASK_PULL_THREAD] Got tasks: {}".format(len(tasks)))
                    task_recv_counter += len(tasks)
                    for task in tasks:
                        self.pending_task_queue.put(task)
                        # logger.debug("[TASK_PULL_THREAD] Ready tasks : {}".format(
                        #    [i['task_id'] for i in self.pending_task_queue]))
            else:
                logger.debug("[TASK_PULL_THREAD] No incoming tasks")

    def push_results(self, kill_event):
        """ Listens on the pending_result_queue and sends out results via 0mq

        Parameters:
        -----------
        kill_event : threading.Event
              Event to let the thread know when it is time to die.
        """

        # We set this timeout so that the thread checks the kill_event and does not
        # block forever on the internal result queue
        timeout = 0.1
        # timer = time.time()
        logger.debug("[RESULT_PUSH_THREAD] Starting thread")

        while not kill_event.is_set():
            time.sleep(LOOP_SLOWDOWN)
            try:
                result = self.pending_result_queue.get(block=True, timeout=timeout)
                self.result_outgoing.send(result)
                logger.debug("[RESULT_PUSH_THREAD] Sent result:{}".format(result))

            except queue.Empty:
                logger.debug("[RESULT_PUSH_THREAD] No results to send in past {}seconds".format(timeout))

            except Exception as e:
                logger.exception("[RESULT_PUSH_THREAD] Got an exception : {}".format(e))

    def start(self):
        """ Start the Daimyo process.

        The worker loops on this:

        1. If the last message sent was older than heartbeat period we send a heartbeat
        2.


        TODO: Move task receiving to a thread
        """

        self.comm.Barrier()
        logger.debug("Daimyo synced with workers")

        self._kill_event = threading.Event()
        self._task_puller_thread = threading.Thread(target=self.pull_tasks,
                                                    args=(self._kill_event,))
        self._result_pusher_thread = threading.Thread(target=self.push_results,
                                                      args=(self._kill_event,))
        self._task_puller_thread.start()
        self._result_pusher_thread.start()

        start = None
        abort_flag = False

        result_counter = 0
        task_recv_counter = 0
        task_sent_counter = 0

        logger.info("Loop start")
        while not abort_flag:
            time.sleep(LOOP_SLOWDOWN)

            # In this block we attempt to probe MPI for a set amount of time,
            # and if we have exhausted all available MPI events, we move on
            # to the next block. The timer and counter trigger balance
            # fairness and responsiveness.
            timer = time.time() + 0.05
            counter = min(10, comm.size)
            while time.time() < timer:
                info = MPI.Status()

                if counter > 10:
                    logger.debug("Hit max mpi events per round")
                    break

                if not self.comm.Iprobe(status=info):
                    logger.debug("Timer expired, processed {} mpi events".format(counter))
                    break
                else:
                    tag = info.Get_tag()
                    logger.info("Message with tag {} received".format(tag))

                    counter += 1
                    if tag == RESULT_TAG:
                        result = self.recv_result_from_workers()
                        self.pending_result_queue.put(result)
                        result_counter += 1

                    elif tag == TASK_REQUEST_TAG:
                        worker_rank = self.recv_task_request_from_workers()
                        self.ready_worker_queue.put(worker_rank)

                    else:
                        logger.error("Unknown tag {} - ignoring this message and continuing".format(tag))

            available_worker_cnt = self.ready_worker_queue.qsize()
            available_task_cnt = self.pending_task_queue.qsize()
            logger.debug("[MAIN] Ready workers: {} Ready tasks: {}".format(available_worker_cnt,
                                                                           available_task_cnt))
            this_round = min(available_worker_cnt, available_task_cnt)
            for i in range(this_round):
                worker_rank = self.ready_worker_queue.get()
                task = self.pending_task_queue.get()
                comm.send(task, dest=worker_rank, tag=worker_rank)
                task_sent_counter += 1
                logger.debug("Assigning Worker:{} task:{}".format(worker_rank, task['task_id']))

            if not start:
                start = time.time()

            logger.debug("Tasks recvd:{} Tasks dispatched:{} Results recvd:{}".format(
                task_recv_counter, task_sent_counter, result_counter))
            # print("[{}] Received: {}".format(self.identity, msg))
            # time.sleep(random.randint(4,10)/10)
            if self._kill_event.is_set():
                logger.debug("Fabric received kill message. Initiating exit")
                break

        self._task_puller_thread.join()
        self._result_pusher_thread.join()

        delta = time.time() - start
        logger.info("Fabric ran for {} seconds".format(delta))


def execute_task(bufs):
    """Deserialize the buffer and execute the task.

    Returns the serialized result or exception.
    """
    user_ns = locals()
    user_ns.update({'__builtins__': __builtins__})

    f, args, kwargs = unpack_apply_message(bufs, user_ns, copy=False)

    fname = getattr(f, '__name__', 'f')
    prefix = "parsl_"
    fname = prefix + "f"
    argname = prefix + "args"
    kwargname = prefix + "kwargs"
    resultname = prefix + "result"

    user_ns.update({fname: f,
                    argname: args,
                    kwargname: kwargs,
                    resultname: resultname})

    code = "{0} = {1}(*{2}, **{3})".format(resultname, fname,
                                           argname, kwargname)

    try:
        logger.debug("[RUNNER] Executing: {0}".format(code))
        exec(code, user_ns, user_ns)

    except Exception as e:
        logger.warning("Caught exception; will raise it: {}".format(e))
        raise e

    else:
        logger.debug("[RUNNER] Result: {0}".format(user_ns.get(resultname)))
        return user_ns.get(resultname)


def worker(comm, rank):
    logger.info("Worker started")

    # Sync worker with master
    comm.Barrier()
    logger.debug("Synced")

    task_request = b'TREQ'

    while True:
        comm.send(task_request, dest=0, tag=TASK_REQUEST_TAG)
        # The worker will receive {'task_id':<tid>, 'buffer':<buf>}
        req = comm.recv(source=0, tag=rank)
        logger.debug("Got req: {}".format(req))
        tid = req['task_id']
        logger.debug("Got task : {}".format(tid))

        try:
            result = execute_task(req['buffer'])
        except Exception as e:
            result_package = {'task_id': tid, 'exception': serialize_object(e)}
            logger.debug("No result due to exception: {} with result package {}".format(e, result_package))
        else:
            result_package = {'task_id': tid, 'result': serialize_object(result)}
            logger.debug("Result : {}".format(result))

        pkl_package = pickle.dumps(result_package)
        comm.send(pkl_package, dest=0, tag=RESULT_TAG)


def start_file_logger(filename, rank, name='parsl', level=logging.DEBUG, format_string=None):
    """Add a stream log handler.

    Args:
        - filename (string): Name of the file to write logs to
        - name (string): Logger name
        - level (logging.LEVEL): Set the logging level.
        - format_string (string): Set the format string

    Returns:
       -  None
    """
    if format_string is None:
        format_string = "%(asctime)s %(name)s:%(lineno)d Rank:{0} [%(levelname)s]  %(message)s".format(rank)

    global logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(filename)
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def set_stream_logger(name='parsl', level=logging.DEBUG, format_string=None):
    """Add a stream log handler.

    Args:
         - name (string) : Set the logger name.
         - level (logging.LEVEL) : Set to logging.DEBUG by default.
         - format_string (sting) : Set to None by default.

    Returns:
         - None
    """
    if format_string is None:
        format_string = "%(asctime)s %(name)s [%(levelname)s] Thread:%(thread)d %(message)s"
        # format_string = "%(asctime)s %(name)s:%(lineno)d [%(levelname)s]  %(message)s"

    global logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    parser.add_argument("-l", "--logdir", default="parsl_worker_logs",
                        help="Parsl worker log directory")
    parser.add_argument("-u", "--uid", default=str(uuid.uuid4()).split('-')[-1],
                        help="Unique identifier string for Daimyo")
    parser.add_argument("-t", "--task_url", required=True,
                        help="REQUIRED: ZMQ url for receiving tasks")
    parser.add_argument("-r", "--result_url", required=True,
                        help="REQUIRED: ZMQ url for posting results")

    args = parser.parse_args()

    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    print("Start rank :", rank)

    try:
        os.makedirs(args.logdir)
    except FileExistsError:
        pass

    # set_stream_logger()
    try:
        if rank == 0:
            start_file_logger('{}/mpi_rank.{}.log'.format(args.logdir, rank),
                              rank,
                              level=logging.DEBUG if args.debug is True else logging.INFO)

            logger.info("Python version :{}".format(sys.version))
            daimyo = Daimyo(comm, rank,
                            task_q_url=args.task_url,
                            result_q_url=args.result_url,
                            uid=args.uid)
            daimyo.start()
        else:
            start_file_logger('{}/mpi_rank.{}.log'.format(args.logdir, rank),
                              rank,
                              level=logging.DEBUG if args.debug is True else logging.INFO)
            worker(comm, rank)
    except Exception as e:
        logger.warning("Fabric exiting")
        logger.exception("Caught error : {}".format(e))
        raise

    logger.debug("Fabric exiting")
    print("Fabric exiting.")
