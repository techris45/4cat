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

        log_file = Path(config.get("PATH_ROOT"), config.get("PATH_LOGS"), "restart-backend.log")
        if not log_file.exists():
            log_stream = log_file.open("w")
            log_stream.write("Initiating 4CAT restart worker\n")
        else:
            log_stream = log_file.open("a")

        if is_resuming:
            # 4CAT was restarted
            # The log file is used by other parts of 4CAT to see how it went,
            # so use it to report the outcome.
            log_stream.write("4CAT restarted.\n")
            version_file = Path(config.get("PATH_ROOT"), ".current_version")
            if version_file.exists():
                log_stream.write("4CAT is now running version %s.\n" % version_file.open().readline().strip())
            log_stream.write("[4CAT] Success. 4CAT restarted and/or upgraded.")

        else:
            # trigger a restart and/or upgrade
            # returns a JSON with a 'status' key and a message, the message
            # being the process output
            os.chdir(config.get("PATH_ROOT"))
            if self.job.remote_id == "upgrade":
                command = sys.executable + " helper-scripts/migrate.py --release --repository %s --yes --restart" % \
                          shlex.quote(config.get("4cat.github_url"))
            else:
                command = sys.executable + " 4cat-daemon.py --no-version-check force-restart"

            try:
                # the tricky part is that this command will interrupt the
                # daemon, i.e. this worker!
                # so we'll never get to actually send a response, if all goes
                # well.
                response = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          check=True, cwd=config.get("PATH_ROOT"))
                if response.returncode != 0:
                    raise RuntimeError("Unexpected return code %s" % str(response.returncode))

                # if we reach this, 4CAT was never restarted, and so the job failed
                log_stream.write(response.stdout.decode("utf-8"))
                log_stream.write("\n[4CAT] Error while restarting 4CAT. Unexpected outcome of restart call. 4CAT was "
                                 "not restarted properly. You may need to do so manually.")
                self.job.finish()

            except (RuntimeError, subprocess.CalledProcessError) as e:
                log_stream.write(e)
                log_stream.write("[4CAT] Error while restarting 4CAT. The script returned a non-standard error code "
                                 "(see above). You may need to restart 4CAT manually.")
                self.job.finish()
