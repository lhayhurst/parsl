"""HighThroughputExecutor builds on the Swift/T EMEWS architecture to use MPI for fast task distribution
"""

from concurrent.futures import Future
import logging
import uuid
import threading
import queue
from multiprocessing import Process, Queue

from ipyparallel.serialize import pack_apply_message  # ,unpack_apply_message
from ipyparallel.serialize import deserialize_object  # ,serialize_object

from parsl.executors.high_throughput import zmq_pipes
from parsl.executors.high_throughput import interchange
from parsl.executors.errors import *
from parsl.executors.base import ParslExecutor
from parsl.dataflow.error import ConfigurationError

from parsl.utils import RepresentationMixin
from parsl.providers import LocalProvider

logger = logging.getLogger(__name__)

BUFFER_THRESHOLD = 1024 * 1024
ITEM_THRESHOLD = 1024


class HighThroughputExecutor(ParslExecutor, RepresentationMixin):
    """Executor designed for cluster-scale

    The HighThroughputExecutor system has 3 components:
      1. The HighThroughputExecutor instance which is run as part of the Parsl script.
      2. The Interchange which is acts as a load-balancing proxy between workers and Parsl
      3. The multiprocessing based fabric which coordinates task execution over several cores on a node.
      4. ZeroMQ pipes connect the HighThroughputExecutor, Interchange and the process_worker_pool

    Here is a diagram

    .. code:: python


                        |  Data   |  Executor   |  Interchange  | External Process(es)
                        |  Flow   |             |               |
                   Task | Kernel  |             |               |
                 +----->|-------->|------------>|->outgoing_q---|-> process_worker_pool
                 |      |         |             | batching      |    |         |
           Parsl<---Fut-|         |             | load-balancing|  result   exception
                     ^  |         |             | watchdogs     |    |         |
                     |  |         |   Q_mngmnt  |               |    V         V
                     |  |         |    Thread<--|-incoming_q<---|--- +---------+
                     |  |         |      |      |               |
                     |  |         |      |      |               |
                     +----update_fut-----+


    Parameters
    ----------

    provider : :class:`~parsl.providers.provider_base.ExecutionProvider`
       Provider to access computation resources. Can be one of :class:`~parsl.providers.aws.aws.EC2Provider`,
        :class:`~parsl.providers.cobalt.cobalt.Cobalt`,
        :class:`~parsl.providers.condor.condor.Condor`,
        :class:`~parsl.providers.googlecloud.googlecloud.GoogleCloud`,
        :class:`~parsl.providers.gridEngine.gridEngine.GridEngine`,
        :class:`~parsl.providers.jetstream.jetstream.Jetstream`,
        :class:`~parsl.providers.local.local.Local`,
        :class:`~parsl.providers.sge.sge.GridEngine`,
        :class:`~parsl.providers.slurm.slurm.Slurm`, or
        :class:`~parsl.providers.torque.torque.Torque`.
    label : str
        Label for this executor instance.
    engine_debug : Bool
        Enables engine debug logging

    public_ip : string
        Please set the public ip of the machine on which Parsl is executing

    worker_ports : (int, int)
        Specify the ports to be used by workers to connect to Parsl. If this option is specified,
        worker_port_range will not be honored.

    worker_port_range : (int, int)
        Worker ports will be chosen between the two integers provided

    interchange_port_range : (int, int)
        Port range used by Parsl to communicate with internal services.

    """

    def __init__(self,
                 label='HighThroughputExecutor',
                 provider=LocalProvider(),
                 launch_cmd=None,
                 public_ip="127.0.0.1",
                 worker_ports=None,
                 worker_port_range=(54000, 55000),
                 interchange_port_range=(55000, 56000),
                 storage_access=None,
                 working_dir=None,
                 engine_debug=False,
                 mock=False,
                 managed=True):

        logger.debug("Initializing HighThroughputExecutor")

        self.label = label
        self.launch_cmd = launch_cmd
        self.mock = mock
        self.provider = provider
        self.engine_debug = engine_debug
        self.storage_access = storage_access if storage_access is not None else []
        if len(self.storage_access) > 1:
            raise ConfigurationError('Multiple storage access schemes are not supported')
        self.working_dir = working_dir
        self.managed = managed
        self.engines = []
        self.tasks = {}

        self.public_ip = public_ip
        self.worker_ports = worker_ports
        self.worker_port_range = worker_port_range
        self.interchange_port_range = interchange_port_range

        if not launch_cmd:
            self.launch_cmd = """process_worker_pool.py {debug} -w {tasks_per_node} --task_url={task_url} --result_url={result_url}"""

    def start(self):
        """ Here we create the Interchange process and connect to it.
        """
        self.outgoing_q = zmq_pipes.TasksOutgoing("127.0.0.1", self.interchange_port_range)
        self.incoming_q = zmq_pipes.ResultsIncoming('127.0.0.1', self.interchange_port_range)

        self.is_alive = True

        self._queue_management_thread = None
        self._start_queue_management_thread()
        self._start_local_queue_process()

        logger.debug("Created management thread : %s", self._queue_management_thread)

        if self.provider:
            debug_opts = "--debug" if self.engine_debug else ""
            l_cmd = self.launch_cmd.format(debug=debug_opts,
                                           task_url=self.worker_task_url,
                                           result_url=self.worker_result_url,
                                           tasks_per_node=self.provider.tasks_per_node,
                                           nodes_per_block=self.provider.nodes_per_block)
            self.launch_cmd = l_cmd
            logger.debug("Launch command :{}".format(self.launch_cmd))

            self._scaling_enabled = self.provider.scaling_enabled
            logger.debug("Starting HighThroughputExecutor with provider:\n%s", self.provider)
            if hasattr(self.provider, 'init_blocks'):
                try:
                    for i in range(self.provider.init_blocks):
                        engine = self.provider.submit(self.launch_cmd, 1)
                        logger.debug("Launched block: {0}:{1}".format(i, engine))
                        if not engine:
                            raise(ScalingFailed(self.provider.label,
                                                "Attempts to provision nodes via provider has failed"))
                        self.engines.extend([engine])

                except Exception as e:
                    logger.error("Scaling out failed: %s" % e)
                    raise e

        else:
            self._scaling_enabled = False
            logger.debug("Starting HighThroughputExecutor with no provider")

    def _queue_management_worker(self):
        """Listen to the queue for task status messages and handle them.

        Depending on the message, tasks will be updated with results, exceptions,
        or updates. It expects the following messages:

        .. code:: python

            {
               "task_id" : <task_id>
               "result"  : serialized result object, if task succeeded
               ... more tags could be added later
            }

            {
               "task_id" : <task_id>
               "exception" : serialized exception object, on failure
            }

        We do not support these yet, but they could be added easily.

        .. code:: python

            {
               "task_id" : <task_id>
               "cpu_stat" : <>
               "mem_stat" : <>
               "io_stat"  : <>
               "started"  : tstamp
            }

        The `None` message is a die request.
        """
        logger.debug("[MTHREAD] queue management worker starting")
        while True:
            try:
                msg = self.incoming_q.get(timeout=1)
                logger.debug("[MTHREAD] get has returned")

            except queue.Empty as e:
                logger.debug("[MTHREAD] queue empty")
                # Timed out.
                pass

            except IOError as e:
                logger.exception("[MTHREAD] Caught broken queue with exception code {}: {}".format(e.errno, e))
                return

            except Exception as e:
                logger.exception("[MTHREAD] Caught unknown exception: {}".format(e))
                return

            else:

                if msg is None:
                    logger.debug("[MTHREAD] Got None, exiting")
                    return

                else:
                    try:
                        tid = msg['task_id']
                    except Exception as e:
                        raise BadMessage("Message received does not contain 'task_id' field")

                    task_fut = self.tasks[tid]

                    if 'result' in msg:
                        result, _ = deserialize_object(msg['result'])
                        task_fut.set_result(result)

                    elif 'exception' in msg:
                        try:
                            exception, _ = deserialize_object(msg['exception'])
                            task_fut.set_exception(exception)
                        except Exception as e:
                            # TODO could be a proper wrapped exception?
                            task_fut.set_exception(
                                DeserializationError("Received exception, but handling also threw an exception: {}".format(e)))
                    else:
                        raise BadMessage("Message received is neither result or exception")

            if not self.is_alive:
                break
        logger.info("[MTHREAD] queue management worker finished")

    # When the executor gets lost, the weakref callback will wake up
    # the queue management thread.
    def weakref_cb(self, q=None):
        """We do not use this yet."""
        q.put(None)

    def _start_local_queue_process(self):

        comm_q = Queue(maxsize=10)
        self.queue_proc = Process(target=interchange.starter,
                                  args=(comm_q,),
                                  kwargs={"client_ports": (self.outgoing_q.port, self.incoming_q.port),
                                          "worker_ports": self.worker_ports,
                                          "worker_port_range": self.worker_port_range
                                  },
        )
        self.queue_proc.start()
        try:
            (worker_task_port, worker_result_port) = comm_q.get(block=True, timeout=120)
        except queue.Empty:
            logger.error("Interchange has not completed initialization in 120s. Aborting")
            raise Exception("Interchange failed to start")

        self.worker_task_url = "tcp://{}:{}".format(self.public_ip, worker_task_port)
        self.worker_result_url = "tcp://{}:{}".format(self.public_ip, worker_result_port)

    def _start_queue_management_thread(self):
        """Method to start the management thread as a daemon.

        Checks if a thread already exists, then starts it.
        Could be used later as a restart if the management thread dies.
        """
        if self._queue_management_thread is None:
            logger.debug("Starting queue management thread")
            self._queue_management_thread = threading.Thread(target=self._queue_management_worker)
            self._queue_management_thread.daemon = True
            self._queue_management_thread.start()
            logger.debug("Started queue management thread")

        else:
            logger.debug("Management thread already exists, returning")

    def submit(self, func, *args, **kwargs):
        """Submits work to the the outgoing_q.

        The outgoing_q is an external process listens on this
        queue for new work. This method is simply pass through and behaves like a
        submit call as described here `Python docs: <https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor>`_

        Args:
            - func (callable) : Callable function
            - *args (list) : List of arbitrary positional arguments.

        Kwargs:
            - **kwargs (dict) : A dictionary of arbitrary keyword args for func.

        Returns:
              Future
        """
        task_id = uuid.uuid4()

        logger.debug("Pushing function {} to queue with args {}".format(func, args))

        self.tasks[task_id] = Future()

        fn_buf = pack_apply_message(func, args, kwargs,
                                    buffer_threshold=1024 * 1024,
                                    item_threshold=1024)

        msg = {"task_id": task_id,
               "buffer": fn_buf}

        # Post task to the the outgoing queue
        self.outgoing_q.put(msg)

        # Return the future
        return self.tasks[task_id]

    @property
    def scaling_enabled(self):
        return self._scaling_enabled

    def scale_out(self, workers=1):
        """Scales out the number of active workers by 1.

        This method is not implemented for threads and will raise the error if called.
        This would be nice to have, and can be done

        Raises:
             NotImplementedError
        """
        if self.provider:
            print("[TODO] Scaling out")
            r = self.provider.submit(self.launch_cmd)
            self.engines.extend([r])
        else:
            logger.error("No execution provider available")
            r = None

        return r

    def scale_in(self, blocks):
        """Scale in the number of active blocks by specified amount.

        This method is not implemented for turbine and will raise an error if called.

        Raises:
             NotImplementedError
        """
        to_kill = self.engines[:blocks]
        if self.provider:
            r = self.provider.cancel(to_kill)
        return r

    def shutdown(self, hub=True, targets='all', block=False):
        """Shutdown the executor, including all workers and controllers.

        This is not implemented.

        Kwargs:
            - hub (Bool): Whether the hub should be shutdown, Default:True,
            - targets (list of ints| 'all'): List of engine id's to kill, Default:'all'
            - block (Bool): To block for confirmations or not

        Raises:
             NotImplementedError
        """

        logger.warning("Attempting MPIX shutdown")
        # self.outgoing_q.close()
        # self.incoming_q.close()
        self.queue_proc.terminate()
        logger.warning("Finished MPIX shutdown attempt")
        return True


if __name__ == "__main__":

    print("Start")
    turb_x = HighThroughputExecutor()
    print("Done")
