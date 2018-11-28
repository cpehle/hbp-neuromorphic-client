"""
Job request (using NMPI API) and execution (using SAGA)

0. This script is called by a cron job
1. it uses the nmpi api to retrieve the next nmpi_job (FIFO of nmpi_job with status='submitted')
2. reads the content of the nmpi_job
3. creates a folder for the nmpi_job
4. obtains the experiment source code specified in the nmpi_job description
5. retrieves input data, if any
7. submits the job to the cluster with SAGA
8. waits for the answer and updates the log and status of the nmpi_job
9. checks for newly created files in the nmpi_job folder and adds them to the list of nmpi_job output data
10. final nmpi_job status modification to 'finished' or 'error'

Authors: Domenico Guarino,
         Andrew Davison

All the personalization should happen in the config file.

"""

import os
from os import path
import logging
from urlparse import urlparse
from urllib import urlretrieve
import shutil
from datetime import datetime
import time
import saga
import subprocess
import nmpi
import codecs
import requests
from requests.auth import AuthBase


DEFAULT_SCRIPT_NAME = "run.py {system}"
DEFAULT_PYNN_VERSION = "0.7"
MAX_LOG_SIZE = 10000

logger = logging.getLogger("NMPI")

# status functions
def job_pending(nmpi_job, saga_job):
    nmpi_job['status'] = "submitted"
    log = nmpi_job.pop("log", str())
    log += "Job ID: {}\n".format(saga_job.id)
    log += "{}    pending\n".format(datetime.now().isoformat())
    nmpi_job["log"] = log
    return nmpi_job


def job_running(nmpi_job, saga_job):
    nmpi_job['status'] = "running"
    log = nmpi_job.pop("log", str())
    log += "{}    running\n".format(datetime.now().isoformat())
    nmpi_job["log"] = log
    return nmpi_job


def truncate_string(stream, max_length):
    """
    """
    if len(stream) > max_length:
        return stream[:max_length//2] + "\n\n... truncated...\n\n" + stream[-max_length//2:]
    else:
        return stream


def job_done(nmpi_job, saga_job):
    nmpi_job['status'] = "finished"
    timestamp = datetime.now().isoformat()
    nmpi_job['timestamp_completion'] = timestamp
    nmpi_job['resource_usage'] = 1.0  # todo: report the actual usage
    nmpi_job['provenance'] = {}  # todo: report provenance information
    log = nmpi_job.pop("log", str())
    log += "{}    finished\n".format(datetime.now().isoformat())
    stdout, stderr = read_output(saga_job)
    log += "\n\n"
    log += truncate_string(stdout, MAX_LOG_SIZE)
    log += "\n\n"
    log += truncate_string(stderr, MAX_LOG_SIZE)
    nmpi_job["log"] = log
    return nmpi_job


def job_failed(nmpi_job, saga_job):
    nmpi_job['status'] = "error"
    log = nmpi_job.pop("log", str())
    log += "{}    failed\n\n".format(datetime.now().isoformat())
    stdout, stderr = read_output(saga_job)
    log += truncate_string(stdout, MAX_LOG_SIZE)
    log += "\n\nstdout\n------\n\n"
    log += truncate_string(stderr, MAX_LOG_SIZE)
    nmpi_job["log"] = log
    return nmpi_job


# states switch
default_job_states = {
    saga.job.PENDING: job_pending,
    saga.job.RUNNING: job_running,
    saga.job.DONE: job_done,
    saga.job.FAILED: job_failed,
}


def load_config(fullpath):
    """
    NOTE: This should be replaced with a standard config format, such as yaml.
    There is no point in implementing a bespoke config format here.
    """
    conf = {}
    with open(fullpath) as f:
        for line in f:
            # leave out comment as python/bash
            if not line.startswith('#') and len(line) >= 5:
                (key, val) = line.split('=')
                conf[key.strip()] = val.strip()
    for key, val in conf.items():
        if val in ("True", "False", "None"):
            conf[key] = eval(val)
    logger.debug("Loaded configuration file '{}' with contents: {}".format(fullpath, conf))
    return conf


class NMPAuth(AuthBase):
    """Attaches ApiKey Authentication to the given Request object."""

    def __init__(self, username, token):
        # setup any auth-related data here
        self.username = username
        self.token = token

    def __call__(self, r):
        # modify and return the request
        r.headers['Authorization'] = 'ApiKey ' + self.username + ":" + self.token
        return r


class HardwareClient(nmpi.Client):
    """
    Client for interacting from a specific hardware, with the Neuromorphic Computing Platform of the Human Brain Project.

    This includes submitting jobs, tracking job status, retrieving the results of completed jobs,
    and creating and administering projects.

    Arguments
    ---------

    username, password : credentials for accessing the platform
    entrypoint : the base URL of the platform. Generally the default value should be used.

    """

    def __init__(self, username, platform, token,
                 job_service="https://nmpi.hbpneuromorphic.eu/api/v2/",
                 verify=True):
        self.username = username
        self.cert = None
        self.verify = verify
        self.token = token
        (scheme, netloc, path, params, query, fragment) = urlparse(job_service)
        self.job_server = "%s://%s" % (scheme, netloc)
        self.auth = NMPAuth(self.username, self.token)
        # get schema
        req = requests.get(job_service, cert=self.cert, verify=self.verify, auth=self.auth)
        if req.ok:
            self._schema = req.json()
            self.resource_map = {name: entry["list_endpoint"]
                                 for name, entry in req.json().items()}
        else:
            self._handle_error(req)
        self.platform = platform

    def get_next_job(self):
        """
        Get the next job by oldest date in the queue.
        """
        job_nmpi = self._query(self.job_server + self.resource_map["queue"] + "/submitted/next/" + self.platform + "/")
        if 'warning' in job_nmpi:
            job_nmpi = None
        return job_nmpi

    def update_job(self, job):
        log = job.pop("log", None)
        response = self._put(self.job_server + job["resource_uri"], job)
        if log:
            log_response = self._put(self.job_server + "/api/v2/log/{}".format(job["id"]),
                                     {"content": log})
        return response

    def reset_job(self, job):
        """
        If a job is stuck in the "running" state due to a problem on the backend,
        reset its status to "submitted".
        """
        job["status"] = "submitted"
        log_response = self._put(self.job_server + "/api/v2/log/{}".format(job["id"]),
                                 {"content": "reset status to 'submitted'\n"})
        return self._put(self.job_server + job["resource_uri"], job)

    def kill_job(self, job, error_message=""):
        """
        Set the status of a queued or running job to "error".

        This should be used circumspectly. It is usually better to use
        `reset_job()`.
        """
        if job["status"] not in ("running", "submitted"):
            raise Exception("You cannot kill a job with status {}".format(job["status"]))
        job["status"] = "error"
        log = job.pop("log", "")
        response = self._put(self.job_server + job["resource_uri"], job)
        log += "Internal error. Please resubmit the job\n"
        log += error_message
        log_response = self._put(self.job_server + "/api/v2/log/{}".format(job["id"]),
                                 {"content": log})
        return response

    def queued_jobs(self, verbose=False):
        """
        Return the list of submitted jobs for the current platform.

        Arguments
        ---------

        verbose : if False, return just the job URIs,
                  if True, return full details.
        """
        return self._query(self.job_server + self.resource_map["queue"] + "/submitted/?hardware_platform=" + str(self.platform),
                           verbose=verbose)

    def running_jobs(self, verbose=False):
        """
        Return the list of running jobs for the current platform.

        Arguments
        ---------

        verbose : if False, return just the job URIs,
                  if True, return full details.
        """
        return self._query(self.job_server + self.resource_map["queue"] + "/running/?hardware_platform=" + str(self.platform),
                           verbose=verbose)


# adapted from Sumatra
def _find_new_data_files(root, timestamp,
                         ignoredirs=[".smt", ".hg", ".svn", ".git", ".bzr"],
                         ignore_extensions=[".pyc"]):
    """Finds newly created/changed files in root.

    NOTE: This is a potentially pretty expensive operation.
    """
    length_root = len(root) + len(path.sep)
    new_files = []
    for root, dirs, files in os.walk(root):
        for igdir in ignoredirs:
            if igdir in dirs:
                dirs.remove(igdir)
        for file in files:
            if path.splitext(file)[1] not in ignore_extensions:
                full_path = path.join(root, file)
                relative_path = path.join(root[length_root:], file)
                last_modified = os.stat(full_path).st_mtime
                if last_modified >= timestamp:
                    new_files.append(relative_path)
    return new_files

def read_output(saga_job):
    """
    Read and return the contents of the stdout and stderr files
    created by the SAGA job.
    """
    job_desc = saga_job.get_description()
    outfile= path.join(job_desc.working_directory, job_desc.output)
    errfile = path.join(job_desc.working_directory, job_desc.error)
    try:
        with open(outfile) as fp:
            stdout = fp.read()
        with open(errfile) as fp:
            stderr = fp.read()
        return stdout, stderr
    except IOError:
        # weird things can happen...
        return "", ""

def create_working_directory(workdir):
    if not path.exists(workdir):
        os.makedirs(workdir)
    else:
        logger.debug("Directory %s already exists" % workdir)

def get_code(working_directory, nmpi_job, script_name = "run.py"):
    """
    Obtain the code and place it in the working directory.
    If the experiment description is the URL of a Git repository, try to clone it.
    If it is the URL of a zip or .tar.gz archive, download and unpack it.
    Otherwise, the content of "code" is the code: write it to a file.
    """
    # NOTE: The code is potentially in unicode, but 
    # urlparse can only handle ascii, so we violently convert the string
    # to ascii here.
    url_candidate = urlparse(str(nmpi_job['code']))
    if url_candidate.scheme and url_candidate.path.endswith((".tar.gz", ".zip", ".tgz")):
        # NOTE: This assumes that the input is more or less valid, just as the rest
        # of the code.
        url = nmpi_job['code']
        logger.info("Retrieving code from url: {}".format(url))
        create_working_directory(working_directory)
        target = os.path.join(working_directory, os.path.basename(url_candidate.path))
        err = subprocess.call(["curl", url, "-o", target])
        if err:
            msg = "Unable to retrieve code from url: {}".format(url)
            logger.info(msg)
            return msg
        logger.info("Retrieved file from {} to local target {}".format(url, target))
        logger.info("Extracting file {}".format(target))
        if url_candidate.path.endswith((".tar.gz", ".tgz")):
            err = subprocess.call(["tar", "xfz", target, "--directory", working_directory])
            if err:
                msg = "Unable extract tar file, malformed archive?"
                logger.info(msg)
                return msg
            return None
        if url_candidate.path.endswith(".zip"):
            err = subprocess.call(["unzip", "-o", target, "-d", working_directory])
            if err:
                msg = "Unable expand zip file, malformed archive?"
                logger.info(msg)
                return msg
            return None
        
        assert False, "unreachable"
    if url_candidate.scheme in ["http", "https", "ssh"]:
        # This could be a git repository (we don't know yet and don't handle the case of local repositories)
        url = nmpi_job['code']
        err = subprocess.call(["git","clone","--recursive",url, working_directory])
        if not err:
            logger.info("Cloned repository {}".format(url))
            return None
    logger.info("The code field appears to contain a script.")
    try:
        create_working_directory(working_directory)
        with codecs.open(script_name, 'w', encoding='utf8') as job_main_script:
            job_main_script.write(nmpi_job['code'])
    except Exception as exception:
        return "Exception occured while writing script: {}".format(repr(exception))
    return None

def get_input_data(hardware_client, nmpi_job, working_directory):
    """
    Retrieve eventual additional input DataItem
    We assume that the script knows the input files are in the same folder
    """
    if 'input_data' in nmpi_job and len(nmpi_job['input_data']):
        try:
            hardware_client.download_data_url(nmpi_job, working_directory, True)
        except Exception as exception:
            return "Exception occurred while downloading input data: {}".format(repr(exception))
    return None

def handle_output_data(hardware_client, 
                       data_server, 
                       data_directory, 
                       working_directory, 
                       start_time, 
                       nmpi_job):
    """
    Adds the contents of the nmpi_job folder to the list of nmpi_job
    output data

    NOTE: This is potentially a pretty fragile implementation. It would
    be easier to separate the output directory from the directory in which
    the code was cloned or alternatively to keep a list of files and directories
    around that have been present before the code executed.
    """

    new_files = _find_new_data_files(working_directory, start_time)
    output_dir = path.join(data_directory, path.basename(working_directory))
    
    logger.info("Copying files to {}: {}".format(output_dir, ", ".join(new_files)))

    if data_directory != working_directory:
        for new_file in new_files:
            try:
                new_file_path = path.join(output_dir, new_file)
                if not os.path.exists(os.path.dirname(new_file_path)):
                    os.makedirs(os.path.dirname(new_file_path))
                shutil.copyfile(path.join(job_desc.working_directory, new_file),
                                new_file_path)
            except Exception as exception:
                msg = "Failed to copy files : {}".format(repr(exception))
                logger.info(msg)
                return msg

    # append the new output to the list of item data and retrieve it
    # by POSTing to the DataItem list resource
    logger.info("Posting data items")
    for new_file in new_files:
        url = "{}/{}/{}".format(data_server, os.path.basename(working_directory), new_file)
        try:
            resource_uri = hardware_client.create_data_item(url)
            nmpi_job['output_data'].append(resource_uri)
        except Exception as exception:
            msg = "Failed to create data item remotely at {}: {}".format(url, repr(exception))
            logger.info(msg)
            return msg

    # ... and PUTting to the job resource
    try: 
        hardware_client.update_job(nmpi_job)
    except Exception as exception:
        msg = "Failed to update the job reflecting the produced output data: {}".format(repr(exception))
        logger.info(msg)
        return msg
    return None



class JobRunner(object):
    """
    This class is responsible for adapting the nmpi 
    job queue accessible through a restful api with
    the saga scheduling middleware.
    """

    def __init__(self, config):
        self.config = config
        self.service = saga.job.Service(config['JOB_SERVICE_ADAPTOR'])
        self.client = HardwareClient(username=config['AUTH_USER'],
                                     token=config['AUTH_TOKEN'],
                                     job_service=config['NMPI_HOST'] + config['NMPI_API'],
                                     platform=config['PLATFORM_NAME'],
                                     verify=config['VERIFY_SSL'])

    def retrieve_pending_jobs(self):
        """
        Retrieve all pending nmpi jobs and return them in a list.
        """
        pending_jobs = []
        while True:
            nmpi_job = self.client.get_next_job()
            if nmpi_job is None or nmpi_job in pending_jobs:
                break
        return pending_jobs

    def submit_jobs(self, pending_jobs = []):
        """
        Submit a list of pending nmpi jobs to the saga job system.
        If a nmpi job fails to be submitted it is killed on the server.
        Returns a list of tuples containing the nmpi_job and corresponding saga_job.
        """
        saga_jobs = []
        for nmpi_job in pending_jobs:
            saga_job, err = self.run(nmpi_job)
            if err:
                self.client.kill_job(job=nmpi_job, error_message=str(err))
                continue
            self._update_status(nmpi_job, saga_job, default_job_states)
            saga_jobs.append((nmpi_job, saga_job))
        return saga_jobs

    def wait_on_completion(self, pending_jobs = []):
        """
        Wait on the completion of a list of saga jobs.
        """
        while True:
            if pending_jobs is []:
                break
            for nmpi_job, saga_job in pending_jobs:
                saga_job.wait(100)
                state = saga_job.get_state()
                if state == saga.job.DONE:
                    err = self._handle_output_data(nmpi_job, saga_job)
                    if err:
                        self.client.kill_job(job=nmpi_job, error_message=str(err))
                        logger.info("Job {} killed, because of faulty output handling".format(saga_job.id))
                        pending_jobs.remove((nmpi_job, saga_job))
                        continue
                    logger.info("Job {} completed".format(saga_job.id))
                elif state == saga.job.FAILED:
                    logger.info("Job {} failed".format(saga_job.id))
                elif state == saga_job.job.CANCELED:
                    logger.info("Job {} got canceled".format(saga_job.id))
                else:
                    continue

                pending_jobs.remove((nmpi_job, saga_job))
                self._update_status(nmpi_job, saga_job, default_job_states)

    def next(self):
        """
        Get all pending nmpi jobs from the server, submit them using saga 
        and wait for their completion.
        """
        pending_nmpi_jobs = self.retrieve_pending_jobs()
        pending_saga_jobs = self.submit_jobs(pending_nmpi_jobs)
        self.wait_on_completion(pending_saga_jobs)
        return pending_saga_jobs

    def run(self, nmpi_job):
        """
        Run a given nmpi job as a saga job. Returns a tuple
        of the saga_job handle or None and an error message or None.
        """
        # Build the job description
        try:
            job_desc = self._build_job_description(nmpi_job)
        except Exception as exception:
            msg = "Failed to build job description with error: {}".format(repr(exception))
            logger.error(msg)
            return None, msg

        # Get the source code for the experiment
        err = get_code(nmpi_job, job_desc.working_directory, script_name=job_desc.arguments[0])
        if err:
            msg = "Failed to obtain source code: {}".format(err)
            logger.info(msg)
            return None, msg

        # Download any input data
        err = get_input_data(self.client, nmpi_job, job_desc.working_directory)
        if err:
            msg = "Failed to download input data."
            logger.error(msg)
            return None, msg

        # Submit a job to the cluster with SAGA."""
        try: 
            saga_job = self.service.create_job(job_desc)
        except Exception as exception:
            msg = "Failed to create job on cluster with exception: {}".format(repr(exception))
            logger.error(msg)
            return None, msg

        # Run the job
        saga_job.start_time = time.time()
        logger.info("Running job {}".format(nmpi_job['id']))
        try:
            saga_job.run()
        except Exception as exception:
            msg = "Failed to run saga job with exception: {}".format(repr(exception))
            logger.error(msg)
            return None, msg

        return saga_job, ""

    def close(self):
        self.service.close()

    def _build_job_description(self, nmpi_job):
        """
        Construct a Saga job description based on an NMPI job description and
        the local configuration.
        """
        #    Set all relevant parameters as in http://saga-project.github.io/saga-python/doc/library/job/index.html
        #    http://saga-project.github.io/saga-python/doc/tutorial/part5.html

        job_desc = saga.job.Description()
        job_id = nmpi_job['id']
        job_desc.working_directory = path.join(self.config['WORKING_DIRECTORY'], 'job_%s' % job_id)
        # job_desc.spmd_variation    = "MPI" # to be commented out if not using MPI

        if nmpi_job['hardware_config'] is None:
            pyNN_version = DEFAULT_PYNN_VERSION
        else:
            pyNN_version = nmpi_job['hardware_config'].get("pyNN_version", DEFAULT_PYNN_VERSION)

        if pyNN_version == "0.7":
            job_desc.executable = self.config['JOB_EXECUTABLE_PYNN_7']
        elif pyNN_version == "0.8":
            job_desc.executable = self.config['JOB_EXECUTABLE_PYNN_8']
        else:
            raise ValueError("Supported PyNN versions: 0.7, 0.8. {} not supported".format(pyNN_version))

        if self.config['JOB_QUEUE'] is not None:
            job_desc.queue = self.config['JOB_QUEUE']  # aka SLURM "partition"
        script_name = nmpi_job.get("command", "")
        if not script_name:
            script_name = DEFAULT_SCRIPT_NAME
        command_line = script_name.format(system=self.config['DEFAULT_PYNN_BACKEND'])  # TODO: allow choosing backend in "hardware_config
        command_line = path.join(job_desc.working_directory, command_line)
        job_desc.arguments = command_line.split(" ")
        job_desc.output = "saga_" + str(job_id) + '.out'
        job_desc.error = "saga_" + str(job_id) + '.err'
        logger.info(command_line)
        return job_desc

    def _update_status(self, nmpi_job, saga_job, job_states):
        """Update the status of the nmpi job."""
        saga_state = saga_job.get_state()
        logger.debug("SAGA state: {}".format(saga_state))
        set_status = job_states[saga_state]
        nmpi_job = set_status(nmpi_job, saga_job)
        self.client.update_job(nmpi_job)
        return nmpi_job

    def _handle_output_data(self, nmpi_job, saga_job):
        """
        Adds the contents of the nmpi_job folder to the list of nmpi_job
        output data

        NOTE: This is potentially a pretty fragile implementation. It would
        be easier to separate the output directory from the directory in which
        the code was cloned or alternatively to keep a list of files and directories
        around that have been present before the code executed.
        """
        job_desc = saga_job.get_description()
        new_files = _find_new_data_files(job_desc.working_directory, saga_job.start_time)
        output_dir = path.join(self.config['DATA_DIRECTORY'], path.basename(job_desc.working_directory))
        logger.debug("Copying files to {}: {}".format(output_dir,
                                                     ", ".join(new_files)))
        if self.config["DATA_DIRECTORY"] != self.config["WORKING_DIRECTORY"]:
            if not path.exists(self.config['DATA_DIRECTORY']):
                try:
                    os.makedirs(self.config['DATA_DIRECTORY'])
                except Exception as exception:
                    logging.error("Failed to create output directory: {}".format(repr(exception)))
                    return repr(exception)
            for new_file in new_files:
                try:
                    new_file_path = path.join(output_dir, new_file)
                    if not os.path.exists(os.path.dirname(new_file_path)):
                        os.makedirs(os.path.dirname(new_file_path))
                    shutil.copyfile(path.join(job_desc.working_directory, new_file),
                                    new_file_path)
                except Exception as exception:
                    msg = "Failed to copy files : {}".format(repr(exception))
                    logging.info(msg)
                    return msg
        # append the new output to the list of item data and retrieve it
        # by POSTing to the DataItem list resource
        logger.info("Posting data items")
        for new_file in new_files:
            url = "{}/{}/{}".format(self.config["DATA_SERVER"], os.path.basename(job_desc.working_directory), new_file)
            try:
                resource_uri = self.client.create_data_item(url)
                nmpi_job['output_data'].append(resource_uri)
            except Exception as exception:
                msg = "Failed to create data item remotely at {}: {}".format(url, repr(exception))
                logging.info(msg)
                return msg

        # ... and PUTting to the job resource
        try: 
            self.client.update_job(nmpi_job)
        except Exception as exception:
            msg = "Failed to update the job reflecting the produced output data: {}".format(repr(exception))
            logger.info(msg)
            return msg
        return None


def main():
    config = load_config(
        os.environ.get("NMPI_CONFIG",
                       path.join(os.getcwd(), "nmpi.cfg"))
    )
    try:
        runner = JobRunner(config)
    except Exception as exception:
        # NOTE: JobRunner relies on being able to retrieve a schema from the
        # HBP endpoint, this might fail if the server is down.
        # We might retry at this point or simply restart the service after some time.
        logger.error("Failed to initialize JobRunner with exception: {}".format(repr(exception)))
        raise exception
    try:
        runner.next()
    except Exception as exception:
        # NOTE: We attempt to handle the majority of errors internally,
        # this is meant to capture the remaining cases.
        logger.error("Unhandled exception while running: {}".format(repr(exception)))
        raise exception
    return 0

if __name__ == "__main__":
    import sys
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    main()
