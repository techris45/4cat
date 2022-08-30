"""
Restart 4CAT and optionally upgrade it to the latest release
"""
import subprocess
import shlex
import sys
import os

from pathlib import Path

import common.config_manager as config

from backend.abstract.worker import BasicWorker
from common.lib.exceptions import WorkerInterruptedException


class FourcatRestarterAndUpgrader(BasicWorker):
    """
    Restart 4CAT and optionally upgrade it to the latest release

    Why implement this as a worker? Trying to have 4CAT restart itself leads
    to an interesting conundrum: it will not be able to report the outcome of
    the restart, because whatever bit of code is keeping track of that will be
    interrupted by restarting 4CAT.

    Using a worker has the benefit of it restarting after 4CAT restarts, so it
    can then figure out that 4CAT was just restarted and report the outcome. It
    then uses a log file to keep track of the results. The log file can then be
    used by other parts of 4CAT to see if the restart was successful.

    It does lead to another conundrum - what if due to some error, 4CAT never
    restarts? Then this worker will not be run again to report its own failure.
    There seem to be no clean ways around this, so anything watching the
    outcome of the worker probably needs to implement some timeout after which
    it is assumed that the restart/upgrade process failed catastrophically.
    """
    type = "restart-4cat"
    max_workers = 1

    def work(self):
        """
        Restart 4CAT and optionally upgrade it to the latest release
        """
        # figure out if we're starting the restart or checking the result
        # after 4cat has been restarted
        is_resuming = self.job.data["attempts"] > 0

        # this file will keep the output of the process started to restart or
        # upgrade 4cat
        log_file_backend = Path(config.get("PATH_ROOT"), config.get("PATH_LOGS"), "restart-backend.log")

        # this file has the log of the restart worker itself and is checked by
        # the frontend to see how far we are
        log_file_restart = Path(config.get("PATH_ROOT"), config.get("PATH_LOGS"), "restart.log")
        log_stream_restart = log_file_restart.open("a")

        self.log.info("Initiating restart job %s, %s attempts so far" % (str(self.job.data["id"]), str(self.job.data["attempts"])))

        if is_resuming:
            # 4CAT was restarted
            # The log file is used by other parts of 4CAT to see how it went,
            # so use it to report the outcome.
            log_stream_restart.write("4CAT restarted.\n")
            with Path(config.get("PATH_ROOT"), "config/.current-version").open() as infile:
                log_stream_restart.write("4CAT is now running version %s.\n" % infile.readline().strip())

            with log_file_backend.open() as infile:
                # copy output of started process to restart log
                log_stream_restart.write(infile.read() + "\n")

            # log_file_backend.unlink()  # no longer necessary
            log_stream_restart.write("[Worker] Success. 4CAT restarted and/or upgraded.\n")
            log_stream_restart.close()

            self.log.info("Restart worker resumed after restarting 4CAT, restart successful.")
            self.job.finish()

        else:
            log_stream_restart.write("Initiating 4CAT restart worker\n")
            self.log.info("New restart initiated.")

            # trigger a restart and/or upgrade
            # returns a JSON with a 'status' key and a message, the message
            # being the process output
            os.chdir(config.get("PATH_ROOT"))
            #if log_file_backend.exists():
            #    log_file_backend.unlink()
            if self.job.data["remote_id"] == "upgrade":
                command = sys.executable + " helper-scripts/migrate.py --release --repository %s --yes --restart --output %s" % \
                          (shlex.quote(config.get("4cat.github_url")), shlex.quote(str(log_file_backend)))
            else:
                command = sys.executable + " 4cat-daemon.py --no-version-check force-restart"

            try:
                # the tricky part is that this command will interrupt the
                # daemon, i.e. this worker!
                # so we'll never get to actually send a response, if all goes
                # well. but the file descriptor that stdout is piped to remains
                # open, somehow, so we can use that to keep track of the output
                # stdin needs to be /dev/null here because else when 4CAT
                # restarts and we re-attempt to make a daemon, it will fail
                # when trying to close the stdin file descriptor of the
                # subprocess (man, that was a fun bug to hunt down)
                log_stream_backend = log_file_backend.open("a")
                self.log.info("Running command %s" % command)
                process = subprocess.Popen(shlex.split(command), cwd=config.get("PATH_ROOT"),
                                           stdout=log_stream_backend, stderr=log_stream_backend, stdin=subprocess.DEVNULL)

                while not self.interrupted:
                    # basically wait for either the process to quit or 4CAT to
                    # be restarted (hopefully the latter)
                    try:
                        process.wait(1)
                        log_stream_backend.close()
                        break
                    except subprocess.TimeoutExpired:
                        pass

                if process.returncode is not None:
                    # if we reach this, 4CAT was never restarted, and so the job failed
                    log_stream_restart.write("\nUnexpected outcome of restart call (%s).\n" % (repr(process.returncode)))

                    raise RuntimeError()
                else:
                    # interrupted before the process could finish (as it should)
                    self.log.info("Restart triggered. Restarting 4CAT.")
                    raise WorkerInterruptedException()

            except (RuntimeError, subprocess.CalledProcessError) as e:
                log_stream_restart.write(str(e))
                log_stream_restart.write("[Worker] Error while restarting 4CAT. The script returned a non-standard error code "
                                 "(see above). You may need to restart 4CAT manually.\n")
                self.log.error("Error restarting 4CAT. See %s for details." % log_stream_restart.name)
                self.job.finish()

            finally:
                log_stream_restart.close()
