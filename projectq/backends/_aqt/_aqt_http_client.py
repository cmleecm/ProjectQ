#   Copyright 2020, 2021 ProjectQ-Framework (www.projectq.ch)
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Back-end to run quantum program on AQT cloud platform."""

import getpass
import signal
import time

import requests
from requests import Session
from requests.compat import urljoin

from .._exceptions import DeviceOfflineError, DeviceTooSmall, RequestTimeoutError

# An AQT token can be requested at:
# https://gateway-portal.aqt.eu/


_API_URL = 'https://gateway.aqt.eu/marmot/'


class AQT(Session):
    """Class managing the session to AQT's APIs."""

    def __init__(self):
        """Initialize an AQT session with AQT's APIs."""
        super().__init__()
        self.backends = {}
        self.timeout = 5.0
        self.token = None

    def update_devices_list(self, verbose=False):
        """
        Update the internal device list.

        Returns:
            (list): list of available devices

        Note:
            Up to my knowledge there is no proper API call for online devices, so we just assume that the list from
            AQT portal always up to date
        """
        # TODO: update once the API for getting online devices is available
        self.backends = {}
        self.backends['aqt_simulator'] = {'nq': 11, 'version': '0.0.1', 'url': 'sim/'}
        self.backends['aqt_simulator_noise'] = {
            'nq': 11,
            'version': '0.0.1',
            'url': 'sim/noise-model-1',
        }
        self.backends['aqt_device'] = {'nq': 4, 'version': '0.0.1', 'url': 'lint/'}
        if verbose:
            print('- List of AQT devices available:')
            print(self.backends)

    def is_online(self, device):
        """
        Check whether a device is currently online.

        Args:
            device (str): name of the aqt device to use

        Note:
            Useless at the moment, may change if the API evolves
        """
        return device in self.backends

    def can_run_experiment(self, info, device):
        """
        Check if the device is big enough to run the code.

        Args:
            info (dict): dictionary sent by the backend containing the code to
                run
            device (str): name of the aqt device to use
        Returns:
            (bool): True if device is big enough, False otherwise
        """
        nb_qubit_max = self.backends[device]['nq']
        nb_qubit_needed = info['nq']
        return nb_qubit_needed <= nb_qubit_max, nb_qubit_max, nb_qubit_needed

    def authenticate(self, token=None):
        """
        Authenticate with the AQT Web API.

        Args:
            token (str): AQT user API token.
        """
        if token is None:
            token = getpass.getpass(prompt='AQT token > ')
        self.headers.update({'Ocp-Apim-Subscription-Key': token, 'SDK': 'ProjectQ'})
        self.token = token

    def run(self, info, device):
        """Run a quantum circuit."""
        argument = {
            'data': info['circuit'],
            'access_token': self.token,
            'repetitions': info['shots'],
            'no_qubits': info['nq'],
        }
        req = super().put(urljoin(_API_URL, self.backends[device]['url']), data=argument)
        req.raise_for_status()
        r_json = req.json()
        if r_json['status'] != 'queued':
            raise Exception('Error in sending the code online')
        execution_id = r_json["id"]
        return execution_id

    def get_result(  # pylint: disable=too-many-arguments
        self, device, execution_id, num_retries=3000, interval=1, verbose=False
    ):
        """Get the result of an execution."""
        if verbose:
            print(f"Waiting for results. [Job ID: {execution_id}]")

        original_sigint_handler = signal.getsignal(signal.SIGINT)

        def _handle_sigint_during_get_result(*_):  # pragma: no cover
            raise Exception(f"Interrupted. The ID of your submitted job is {execution_id}.")

        try:
            signal.signal(signal.SIGINT, _handle_sigint_during_get_result)

            for retries in range(num_retries):

                argument = {'id': execution_id, 'access_token': self.token}
                req = super().put(urljoin(_API_URL, self.backends[device]['url']), data=argument)
                req.raise_for_status()
                r_json = req.json()
                if r_json['status'] == 'finished' or 'samples' in r_json:
                    return r_json['samples']
                if r_json['status'] != 'running':
                    raise Exception(f"Error while running the code: {r_json['status']}.")
                time.sleep(interval)
                if self.is_online(device) and retries % 60 == 0:
                    self.update_devices_list()

                    # TODO: update once the API for getting online devices is
                    #       available
                    if not self.is_online(device):  # pragma: no cover
                        raise DeviceOfflineError(
                            f"Device went offline. The ID of your submitted job is {execution_id}."
                        )

        finally:
            if original_sigint_handler is not None:
                signal.signal(signal.SIGINT, original_sigint_handler)

        raise RequestTimeoutError(f"Timeout. The ID of your submitted job is {execution_id}.")


def show_devices(verbose=False):
    """
    Access the list of available devices and their properties (ex: for setup configuration).

    Args:
        verbose (bool): If True, additional information is printed

    Returns:
        (list) list of available devices and their properties
    """
    aqt_session = AQT()
    aqt_session.update_devices_list(verbose=verbose)
    return aqt_session.backends


def retrieve(device, token, jobid, num_retries=3000, interval=1, verbose=False):  # pylint: disable=too-many-arguments
    """
    Retrieve a previously run job by its ID.

    Args:
        device (str): Device on which the code was run / is running.
        token (str): AQT user API token.
        jobid (str): Id of the job to retrieve

    Returns:
        (list) samples form the AQT server
    """
    aqt_session = AQT()
    aqt_session.authenticate(token)
    aqt_session.update_devices_list(verbose)
    res = aqt_session.get_result(device, jobid, num_retries=num_retries, interval=interval, verbose=verbose)
    return res


def send(
    info,
    device='aqt_simulator',
    token=None,
    num_retries=100,
    interval=1,
    verbose=False,
):  # pylint: disable=too-many-arguments
    """
    Send circuit through the AQT API and runs the quantum circuit.

    Args:
        info(dict): Contains representation of the circuit to run.
        device (str): name of the aqt device. Simulator chosen by default
        token (str): AQT user API token.
        verbose (bool): If True, additional information is printed, such as measurement statistics. Otherwise, the
            backend simply registers one measurement result (same behavior as the projectq Simulator).

    Returns:
        (list) samples form the AQT server

    """
    try:
        aqt_session = AQT()

        if verbose:
            print("- Authenticating...")
        if token is not None:
            print(f"user API token: {token}")
        aqt_session.authenticate(token)

        # check if the device is online
        aqt_session.update_devices_list(verbose)
        online = aqt_session.is_online(device)
        # useless for the moment
        if not online:  # pragma: no cover
            print("The device is offline (for maintenance?). Use the simulator instead or try again later.")
            raise DeviceOfflineError("Device is offline.")

        # check if the device has enough qubit to run the code
        runnable, qmax, qneeded = aqt_session.can_run_experiment(info, device)
        if not runnable:
            print(
                f"The device is too small ({qmax} qubits available) for the code requested({qneeded} qubits needed).",
                "Try to look for another device with more qubits",
            )
            raise DeviceTooSmall("Device is too small.")
        if verbose:
            print(f"- Running code: {info}")
        execution_id = aqt_session.run(info, device)
        if verbose:
            print("- Waiting for results...")
        res = aqt_session.get_result(
            device,
            execution_id,
            num_retries=num_retries,
            interval=interval,
            verbose=verbose,
        )
        if verbose:
            print("- Done.")
        return res
    except requests.exceptions.HTTPError as err:
        print("- There was an error running your code:")
        print(err)
    except requests.exceptions.RequestException as err:
        print("- Looks like something is wrong with server:")
        print(err)
    except KeyError as err:
        print("- Failed to parse response:")
        print(err)
    return None
