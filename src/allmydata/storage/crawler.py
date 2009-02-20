
import os, time, struct
import cPickle as pickle
from twisted.internet import reactor
from twisted.application import service
from allmydata.storage.server import si_b2a
from allmydata.util import fileutil

class TimeSliceExceeded(Exception):
    pass

class ShareCrawler(service.MultiService):
    """A ShareCrawler subclass is attached to a StorageServer, and
    periodically walks all of its shares, processing each one in some
    fashion. This crawl is rate-limited, to reduce the IO burden on the host,
    since large servers will have several million shares, which can take
    hours or days to read.

    We assume that the normal upload/download/get_buckets traffic of a tahoe
    grid will cause the prefixdir contents to be mostly cached, or that the
    number of buckets in each prefixdir will be small enough to load quickly.
    A 1TB allmydata.com server was measured to have 2.56M buckets, spread
    into the 1040 prefixdirs, with about 2460 buckets per prefix. On this
    server, each prefixdir took 130ms-200ms to list the first time, and 17ms
    to list the second time.

    To use this, create a subclass which implements the process_bucket()
    method. It will be called with a prefixdir and a base32 storage index
    string. process_bucket() should run synchronously. Any keys added to
    self.state will be preserved. Override add_initial_state() to set up
    initial state keys. Override finished_cycle() to perform additional
    processing when the cycle is complete.

    Then create an instance, with a reference to a StorageServer and a
    filename where it can store persistent state. The statefile is used to
    keep track of how far around the ring the process has travelled, as well
    as timing history to allow the pace to be predicted and controlled. The
    statefile will be updated and written to disk after every bucket is
    processed.

    The crawler instance must be started with startService() before it will
    do any work. To make it stop doing work, call stopService() and wait for
    the Deferred that it returns.
    """

    # all three of these can be changed at any time
    allowed_cpu_percentage = .10 # use up to 10% of the CPU, on average
    cpu_slice = 1.0 # use up to 1.0 seconds before yielding
    minimum_cycle_time = 300 # don't run a cycle faster than this

    def __init__(self, server, statefile, allowed_cpu_percentage=None):
        service.MultiService.__init__(self)
        if allowed_cpu_percentage is not None:
            self.allowed_cpu_percentage = allowed_cpu_percentage
        self.server = server
        self.sharedir = server.sharedir
        self.statefile = statefile
        self.prefixes = [si_b2a(struct.pack(">H", i << (16-10)))[:2]
                         for i in range(2**10)]
        self.prefixes.sort()
        self.timer = None
        self.bucket_cache = (None, [])
        self.current_sleep_time = None
        self.next_wake_time = None

    def get_state(self):
        """I return the current state of the crawler. This is a copy of my
        state dictionary, plus the following keys::

         current-sleep-time: float, duration of our current sleep
         next-wake-time: float, seconds-since-epoch of when we will next wake

        If we are not currently sleeping (i.e. get_state() was called from
        inside the process_prefixdir, process_bucket, or finished_cycle()
        methods, or if startService has not yet been called on this crawler),
        these two keys will be None.
        """
        state = self.state.copy() # it isn't a deepcopy, so don't go crazy
        state["current-sleep-time"] = self.current_sleep_time
        state["next-wake-time"] = self.next_wake_time
        return state

    def load_state(self):
        # we use this to store state for both the crawler's internals and
        # anything the subclass-specific code needs. The state is stored
        # after each bucket is processed, after each prefixdir is processed,
        # and after a cycle is complete. The internal keys we use are:
        #  ["version"]: int, always 1
        #  ["last-cycle-finished"]: int, or None if we have not yet finished
        #                           any cycle
        #  ["current-cycle"]: int, or None if we are sleeping between cycles
        #  ["last-complete-prefix"]: str, two-letter name of the last prefixdir
        #                            that was fully processed, or None if we
        #                            are sleeping between cycles, or if we
        #                            have not yet finished any prefixdir since
        #                            a cycle was started
        #  ["last-complete-bucket"]: str, base32 storage index bucket name
        #                            of the last bucket to be processed, or
        #                            None if we are sleeping between cycles
        try:
            f = open(self.statefile, "rb")
            state = pickle.load(f)
            f.close()
        except EnvironmentError:
            state = {"version": 1,
                     "last-cycle-finished": None,
                     "current-cycle": 0,
                     "last-complete-prefix": None,
                     "last-complete-bucket": None,
                     }
        self.state = state
        lcp = state["last-complete-prefix"]
        if lcp == None:
            self.last_complete_prefix_index = -1
        else:
            self.last_complete_prefix_index = self.prefixes.index(lcp)
        self.add_initial_state()

    def add_initial_state(self):
        """Hook method to add extra keys to self.state when first loaded.

        The first time this Crawler is used, or when the code has been
        upgraded, the saved state file may not contain all the keys you
        expect. Use this method to add any missing keys. Simply modify
        self.state as needed.

        This method for subclasses to override. No upcall is necessary.
        """
        pass

    def save_state(self):
        lcpi = self.last_complete_prefix_index
        if lcpi == -1:
            last_complete_prefix = None
        else:
            last_complete_prefix = self.prefixes[lcpi]
        self.state["last-complete-prefix"] = last_complete_prefix
        tmpfile = self.statefile + ".tmp"
        f = open(tmpfile, "wb")
        pickle.dump(self.state, f)
        f.close()
        fileutil.move_into_place(tmpfile, self.statefile)

    def startService(self):
        self.load_state()
        self.timer = reactor.callLater(0, self.start_slice)
        service.MultiService.startService(self)

    def stopService(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        return service.MultiService.stopService(self)

    def start_slice(self):
        self.timer = None
        self.current_sleep_time = None
        self.next_wake_time = None
        start_slice = time.time()
        try:
            self.start_current_prefix(start_slice)
            finished_cycle = True
        except TimeSliceExceeded:
            finished_cycle = False
        # either we finished a whole cycle, or we ran out of time
        now = time.time()
        this_slice = now - start_slice
        # this_slice/(this_slice+sleep_time) = percentage
        # this_slice/percentage = this_slice+sleep_time
        # sleep_time = (this_slice/percentage) - this_slice
        sleep_time = (this_slice / self.allowed_cpu_percentage) - this_slice
        # if the math gets weird, or a timequake happens, don't sleep forever
        sleep_time = max(0.0, min(sleep_time, 299))
        if finished_cycle:
            # how long should we sleep between cycles? Don't run faster than
            # allowed_cpu_percentage says, but also run faster than
            # minimum_cycle_time
            self.sleeping_between_cycles = True
            sleep_time = max(sleep_time, self.minimum_cycle_time)
        else:
            self.sleeping_between_cycles = False
        self.current_sleep_time = sleep_time # for status page
        self.next_wake_time = now + sleep_time
        self.yielding(sleep_time)
        self.timer = reactor.callLater(sleep_time, self.start_slice)

    def start_current_prefix(self, start_slice):
        if self.state["current-cycle"] is None:
            assert self.state["last-cycle-finished"] is not None
            self.state["current-cycle"] = self.state["last-cycle-finished"] + 1
        cycle = self.state["current-cycle"]

        for i in range(self.last_complete_prefix_index+1, len(self.prefixes)):
            if time.time() > start_slice + self.cpu_slice:
                raise TimeSliceExceeded()
            # if we want to yield earlier, just raise TimeSliceExceeded()
            prefix = self.prefixes[i]
            prefixdir = os.path.join(self.sharedir, prefix)
            if i == self.bucket_cache[0]:
                buckets = self.bucket_cache[1]
            else:
                try:
                    buckets = os.listdir(prefixdir)
                    buckets.sort()
                except EnvironmentError:
                    buckets = []
                self.bucket_cache = (i, buckets)
            self.process_prefixdir(cycle, prefix, prefixdir,
                                   buckets, start_slice)
            self.last_complete_prefix_index = i
            self.save_state()
        # yay! we finished the whole cycle
        self.last_complete_prefix_index = -1
        self.state["last-complete-bucket"] = None
        self.state["last-cycle-finished"] = cycle
        self.state["current-cycle"] = None
        self.finished_cycle(cycle)
        self.save_state()

    def process_prefixdir(self, cycle, prefix, prefixdir, buckets, start_slice):
        """This gets a list of bucket names (i.e. storage index strings,
        base32-encoded) in sorted order.

        Override this if your crawler doesn't care about the actual shares,
        for example a crawler which merely keeps track of how many buckets
        are being managed by this server.
        """
        for bucket in buckets:
            if bucket <= self.state["last-complete-bucket"]:
                continue
            if time.time() > start_slice + self.cpu_slice:
                raise TimeSliceExceeded()
            self.process_bucket(cycle, prefix, prefixdir, bucket)
            self.state["last-complete-bucket"] = bucket
            self.save_state()

    def process_bucket(self, cycle, prefix, prefixdir, storage_index_b32):
        """Examine a single bucket. Subclasses should do whatever they want
        to do to the shares therein, then update self.state as necessary.

        This method will be called exactly once per share (per cycle), unless
        the crawler was interrupted (by node restart, for example), in which
        case it might be called a second time on a bucket which was processed
        during the previous node's incarnation. However, in that case, no
        changes to self.state will have been recorded.

        This method for subclasses to override. No upcall is necessary.
        """
        pass

    def finished_cycle(self, cycle):
        """Notify subclass that a cycle (one complete traversal of all
        prefixdirs) has just finished. 'cycle' is the number of the cycle
        that just finished. This method should perform summary work and
        update self.state to publish information to status displays.

        This method for subclasses to override. No upcall is necessary.
        """
        pass

    def yielding(self, sleep_time):
        """The crawler is about to sleep for 'sleep_time' seconds. This
        method is mostly for the convenience of unit tests.

        This method for subclasses to override. No upcall is necessary.
        """
        pass
